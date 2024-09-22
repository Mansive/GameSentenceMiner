import json
import re
import shutil
import tempfile
import time
import keyboard

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import anki
import gametext
import config_reader
import notification
import obs
import offset_updater
import util
import vosk_helper
from anki import update_anki_card, get_last_anki_card
from config_reader import *
from ffmpeg import get_audio_and_trim
from util import *
from vosk_helper import process_audio_with_vosk


def remove_html_tags(text):
    clean_text = re.sub(r'<.*?>', '', text)
    return clean_text


def get_line_timing(last_note):
    if not last_note:
        return gametext.previous_line_time, 0
    line_time = gametext.previous_line_time
    next_line = 0
    try:
        sentence = last_note['fields'][sentence_field]['value']
        if sentence:
            for i, (line, clip_time) in enumerate(reversed(gametext.line_history.items())):
                if remove_html_tags(sentence) in line:
                    line_time = clip_time
                    # next_time = list(clipboard.clipboard_history.values())[-i]
                    # if next_time > clipboard_time:
                    #     next_clipboard = next_time
                    break
    except Exception as e:
        logger.error(f"Using Default clipboard/websocket timing - reason: {e}")

    return line_time, next_line


class VideoToAudioHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory or "Replay" not in event.src_path:
            return
        if event.src_path.endswith(".mkv") or event.src_path.endswith(".mp4"):  # Adjust based on your OBS output format
            logger.info(f"MKV {event.src_path} FOUND, RUNNING LOGIC")
            self.convert_to_audio(event.src_path)

    @staticmethod
    def convert_to_audio(video_path):
        with util.lock:
            util.use_previous_audio = True
            last_note = get_last_anki_card()
            line_time, next_line_time = get_line_timing(last_note)
            if last_note:
                logger.debug(json.dumps(last_note))

            if backfill_audio:
                last_note = anki.get_cards_by_sentence(gametext.previous_line)

            tango = last_note['fields'][word_field]['value'] if last_note else ''

            trimmed_audio = get_audio_and_trim(video_path, line_time, next_line_time)

            output_audio = make_unique_file_name(f"{audio_destination}{config_reader.current_game}.{audio_extension}")
            if do_vosk_postprocessing:
                anki.should_update_audio = process_audio_with_vosk(trimmed_audio, output_audio)
            else:
                shutil.copy2(trimmed_audio, output_audio)

            try:
                # Only update sentenceaudio if it's not present. Want to avoid accidentally overwriting sentence audio
                try:
                    if update_anki and last_note and (not last_note['fields'][sentence_audio_field]['value'] or override_audio):
                        update_anki_card(last_note, output_audio, video_path, tango)
                    else:
                        notification.send_audio_generated_notification(output_audio)
                except Exception as e:
                    logger.error(f"Card failed to update! Maybe it was removed? {e}")
            except FileNotFoundError as f:
                print(f)
                print("Something went wrong with processing, anki card not updated")

            if remove_video and os.path.exists(video_path):
                os.remove(video_path)  # Optionally remove the video after conversion
            if remove_audio and os.path.exists(output_audio):
                os.remove(output_audio)  # Optionally remove the screenshot after conversion


def initialize():
    if not os.path.exists(folder_to_watch):
        os.mkdir(folder_to_watch)
    if not os.path.exists(screenshot_destination):
        os.mkdir(screenshot_destination)
    if not os.path.exists(audio_destination):
        os.mkdir(audio_destination)
    if not os.path.exists("temp_files"):
        os.mkdir("temp_files")
    else:
        for filename in os.listdir("temp_files"):
            file_path = os.path.join("temp_files", filename)
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
    vosk_helper.get_vosk_model()
    obs.start_monitoring_anki()
    if obs_enabled and obs_start_buffer:
        obs.connect_to_obs()
        obs.start_replay_buffer()


def main():
    logger.info("Script started.")
    initialize()
    with tempfile.TemporaryDirectory(dir="temp_files") as temp_dir:
        config_reader.temp_directory = temp_dir
        event_handler = VideoToAudioHandler()
        observer = Observer()
        observer.schedule(event_handler, folder_to_watch, recursive=False)
        observer.start()

        print("Script Initialized. Happy Mining!")
        print(f"Press {offset_reset_hotkey.upper()} to update the audio offsets.")
        keyboard.add_hotkey(offset_reset_hotkey, offset_updater.prompt_for_offset_updates)

        try:
            while util.keep_running:
                time.sleep(1)

        except KeyboardInterrupt:
            util.keep_running = False
            observer.stop()

        if obs_enabled and obs_start_buffer:
            obs.stop_replay_buffer()
            obs.disconnect_from_obs()
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
