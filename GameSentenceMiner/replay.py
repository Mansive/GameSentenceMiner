import os
import shutil
import subprocess
import threading

from watchdog.events import FileSystemEventHandler

from GameSentenceMiner import ffmpeg, util, anki, notification, gametext, configuration, obs
from GameSentenceMiner.configuration import logger, get_config
from GameSentenceMiner.ffmpeg import get_audio_and_trim, get_video_timings
from GameSentenceMiner.gametext import GameLine, get_text_event, get_mined_line
from GameSentenceMiner.util import wait_for_stable_file, make_unique_file_name
from GameSentenceMiner.utility_gui import get_utility_window


# class VideoToAudioHandler(FileSystemEventHandler):
def on_created(self, event):
    if event.is_directory or ("Replay" not in event.src_path and "GSM" not in event.src_path):
        return
    if event.src_path.endswith(".mkv") or event.src_path.endswith(".mp4"):  # Adjust based on your OBS output format
        logger.info(f"MKV {event.src_path} FOUND, RUNNING LOGIC")
        wait_for_stable_file(event.src_path)
        process_replay(event.src_path)

# @staticmethod
def process_replay(video_path):
    try:
        if get_utility_window().line_for_audio:
            line: GameLine = get_utility_window().line_for_audio
            get_utility_window().line_for_audio = None
            if get_config().advanced.audio_player_path:
                audio = get_audio(line, line.next.time if line.next else None, video_path, temporary=True)
                play_audio_in_external(audio)
                os.remove(video_path)
            elif get_config().advanced.video_player_path:
                play_video_in_external(line, video_path)
            return
        if get_utility_window().line_for_screenshot:
            line: GameLine = get_utility_window().line_for_screenshot
            get_utility_window().line_for_screenshot = None
            screenshot = ffmpeg.get_screenshot_for_line(video_path, line)
            os.startfile(screenshot)
            os.remove(video_path)
            return
    except Exception as e:
        logger.error(f"Error Playing Audio/Video: {e}")
        logger.debug(f"Error Playing Audio/Video: {e}", exc_info=True)
        os.remove(video_path)
        return
    try:
        last_note = None
        if card_queue and len(card_queue) > 0:
            last_note = card_queue.pop(0)
        with util.lock:
            util.set_last_mined_line(anki.get_sentence(last_note))
            if os.path.exists(video_path) and os.access(video_path, os.R_OK):
                logger.debug(f"Video found and is readable: {video_path}")

            if get_config().obs.minimum_replay_size and not ffmpeg.is_video_big_enough(video_path,
                                                                                       get_config().obs.minimum_replay_size):
                logger.debug("Checking if video is big enough")
                notification.send_check_obs_notification(reason="Video may be empty, check scene in OBS.")
                logger.error(
                    f"Video was unusually small, potentially empty! Check OBS for Correct Scene Settings! Path: {video_path}")
                return
            if not last_note:
                logger.debug("Attempting to get last anki card")
                if get_config().anki.update_anki:
                    last_note = anki.get_last_anki_card()
                if get_config().features.backfill_audio:
                    last_note = anki.get_cards_by_sentence(gametext.current_line_after_regex)
            line_cutoff = None
            start_line = None
            mined_line = get_text_event(last_note)
            if mined_line:
                start_line = mined_line
                if mined_line.next:
                    line_cutoff = mined_line.next.time

            if get_utility_window().lines_selected():
                lines = get_utility_window().get_selected_lines()
                start_line = lines[0]
                mined_line = get_mined_line(last_note, lines)
                line_cutoff = get_utility_window().get_next_line_timing()

            ss_timing = 0
            if mined_line and line_cutoff or mined_line and get_config().screenshot.use_beginning_of_line_as_screenshot:
                ss_timing = ffmpeg.get_screenshot_time(video_path, mined_line)
            if last_note:
                logger.debug(last_note.to_json())
            selected_lines = get_utility_window().get_selected_lines()
            note = anki.get_initial_card_info(last_note, selected_lines)
            tango = last_note.get_field(get_config().anki.word_field) if last_note else ''
            get_utility_window().reset_checkboxes()

            if get_config().anki.sentence_audio_field:
                logger.debug("Attempting to get audio from video")
                final_audio_output, should_update_audio, vad_trimmed_audio = get_audio(
                    start_line,
                    line_cutoff,
                    video_path)
            else:
                final_audio_output = ""
                should_update_audio = False
                vad_trimmed_audio = ""
                logger.info("No SentenceAudio Field in config, skipping audio processing!")
            if get_config().anki.update_anki and last_note:
                anki.update_anki_card(last_note, note, audio_path=final_audio_output, video_path=video_path,
                                      tango=tango,
                                      should_update_audio=should_update_audio,
                                      ss_time=ss_timing,
                                      game_line=start_line,
                                      selected_lines=selected_lines)
            elif get_config().features.notify_on_update and should_update_audio:
                notification.send_audio_generated_notification(vad_trimmed_audio)
    except Exception as e:
        logger.error(f"Failed Processing and/or adding to Anki: Reason {e}")
        logger.debug(f"Some error was hit catching to allow further work to be done: {e}", exc_info=True)
        notification.send_error_no_anki_update()
    finally:
        if get_config().paths.remove_video and os.path.exists(video_path):
            os.remove(video_path)  # Optionally remove the video after conversion
        if get_config().paths.remove_audio and os.path.exists(vad_trimmed_audio):
            os.remove(vad_trimmed_audio)  # Optionally remove the screenshot after conversion


# @staticmethod
def get_audio(game_line, next_line_time, video_path, temporary=False):
    trimmed_audio = get_audio_and_trim(video_path, game_line, next_line_time)
    if temporary:
        return trimmed_audio
    vad_trimmed_audio = make_unique_file_name(
        f"{os.path.abspath(configuration.get_temporary_directory())}/{obs.get_current_game(sanitize=True)}.{get_config().audio.extension}")
    final_audio_output = make_unique_file_name(os.path.join(get_config().paths.audio_destination,
                                                            f"{obs.get_current_game(sanitize=True)}.{get_config().audio.extension}"))
    should_update_audio = True
    if get_config().vad.do_vad_postprocessing:
        should_update_audio = do_vad_processing(get_config().vad.selected_vad_model, trimmed_audio, vad_trimmed_audio)
        if not should_update_audio:
            should_update_audio = do_vad_processing(get_config().vad.selected_vad_model, trimmed_audio,
                                                    vad_trimmed_audio)
        if not should_update_audio and get_config().vad.add_audio_on_no_results:
            logger.info("No voice activity detected, using full audio.")
            vad_trimmed_audio = trimmed_audio
            should_update_audio = True
    if get_config().audio.ffmpeg_reencode_options and os.path.exists(vad_trimmed_audio):
        ffmpeg.reencode_file_with_user_config(vad_trimmed_audio, final_audio_output,
                                              get_config().audio.ffmpeg_reencode_options)
    elif os.path.exists(vad_trimmed_audio):
        shutil.move(vad_trimmed_audio, final_audio_output)
    return final_audio_output, should_update_audio, vad_trimmed_audio


def do_vad_processing(model, trimmed_audio, vad_trimmed_audio, second_pass=False):
    match model:
        case configuration.OFF:
            pass
        case configuration.SILERO:
            from GameSentenceMiner.vad import silero_trim
            return silero_trim.process_audio_with_silero(trimmed_audio, vad_trimmed_audio)
        case configuration.VOSK:
            from GameSentenceMiner.vad import vosk_helper
            return vosk_helper.process_audio_with_vosk(trimmed_audio, vad_trimmed_audio)
        case configuration.WHISPER:
            from GameSentenceMiner.vad import whisper_helper
            return whisper_helper.process_audio_with_whisper(trimmed_audio, vad_trimmed_audio)


def play_audio_in_external(filepath):
    exe = get_config().advanced.audio_player_path

    filepath = os.path.normpath(filepath)

    command = [exe, filepath]

    try:
        subprocess.Popen(command)
        print(f"Opened {filepath} in {exe}.")
    except Exception as e:
        print(f"An error occurred: {e}")


def play_video_in_external(line, filepath):
    def remove_video_when_closed(p, fp):
        p.wait()
        os.remove(fp)

    command = [get_config().advanced.video_player_path]

    start, _, _ = get_video_timings(filepath, line)

    if start:
        if "vlc" in get_config().advanced.video_player_path:
            command.append("--start-time")
        else:
            command.append("--start")
        command.append(convert_to_vlc_seconds(start))
    command.append(os.path.normpath(filepath))

    logger.info(" ".join(command))

    try:
        proc = subprocess.Popen(command)
        print(f"Opened {filepath} in {get_config().advanced.video_player_path}.")
        threading.Thread(target=remove_video_when_closed, args=(proc, filepath)).start()
    except FileNotFoundError:
        print("VLC not found. Make sure it's installed and in your PATH.")
    except Exception as e:
        print(f"An error occurred: {e}")


def convert_to_vlc_seconds(time_str):
    """Converts HH:MM:SS.milliseconds to VLC-compatible seconds."""
    try:
        hours, minutes, seconds_ms = time_str.split(":")
        seconds, milliseconds = seconds_ms.split(".")
        total_seconds = (int(hours) * 3600) + (int(minutes) * 60) + int(seconds) + (int(milliseconds) / 1000.0)
        return str(total_seconds)
    except ValueError:
        return "Invalid time format"


card_queue = []
