
import os.path
import signal
from datetime import timedelta
from subprocess import Popen

import keyboard
import psutil
import ttkbootstrap as ttk
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem
from watchdog.observers import Observer

from GameSentenceMiner import anki
from GameSentenceMiner import config_gui
from GameSentenceMiner import ffmpeg
from GameSentenceMiner import gametext
from GameSentenceMiner import notification
from GameSentenceMiner import obs
from GameSentenceMiner import util
from GameSentenceMiner.anki import get_note_ids, get_last_anki_card, check_tags_for_should_update, \
    sentence_is_same_as_previous, get_sentence, update_anki_card, get_initial_card_info
from GameSentenceMiner.communication import Message
from GameSentenceMiner.communication.send import send_restart_signal
from GameSentenceMiner.communication.websocket import connect_websocket, register_websocket_message_handler
from GameSentenceMiner.configuration import *
from GameSentenceMiner.downloader.download_tools import download_obs_if_needed, download_ffmpeg_if_needed
from GameSentenceMiner.obs import check_obs_folder_is_correct
from GameSentenceMiner.replay import card_queue, process_replay
from GameSentenceMiner.util import *
from GameSentenceMiner.utility_gui import init_utility_window, get_utility_window

if is_windows():
    import win32api

silero_trim, whisper_helper, vosk_helper = None, None, None
procs_to_close = []
settings_window: config_gui.ConfigApp = None
obs_paused = False
icon: Icon
menu: Menu
root = None

previous_note_ids = set()
first_run = True
last_connection_error = datetime.now()


def initial_checks():
    try:
        subprocess.run(ffmpeg.ffmpeg_base_command_list)
        logger.debug("FFMPEG is installed and accessible.")
    except FileNotFoundError:
        logger.error("FFmpeg not found, please install it and add it to your PATH.")
        raise


def register_hotkeys():
    if get_config().hotkeys.reset_line:
        keyboard.add_hotkey(get_config().hotkeys.reset_line, gametext.reset_line_hotkey_pressed)
    if get_config().hotkeys.take_screenshot:
        keyboard.add_hotkey(get_config().hotkeys.take_screenshot, get_screenshot)
    if get_config().hotkeys.open_utility:
        keyboard.add_hotkey(get_config().hotkeys.open_utility, open_multimine)
    if get_config().hotkeys.play_latest_audio:
        keyboard.add_hotkey(get_config().hotkeys.play_latest_audio, play_most_recent_audio)


def check_for_new_cards():
    global previous_note_ids, first_run, last_connection_error
    current_note_ids = set()
    try:
        current_note_ids = get_note_ids()
    except Exception as e:
        if datetime.now() - last_connection_error > timedelta(seconds=10):
            logger.error(f"Error fetching Anki notes, Make sure Anki is running, ankiconnect add-on is installed, and url/port is configured correctly in GSM Settings")
            last_connection_error = datetime.now()
        return
    new_card_ids = current_note_ids - previous_note_ids
    if new_card_ids and not first_run:
        try:
            update_new_card()
        except Exception as e:
            logger.error("Error updating new card, Reason:", e)
    first_run = False
    previous_note_ids = current_note_ids  # Update the list of known notes


def update_new_card():
    last_card = get_last_anki_card()
    if not last_card or not check_tags_for_should_update(last_card):
        return
    use_prev_audio = sentence_is_same_as_previous(last_card)
    logger.info(f"last mined line: {util.get_last_mined_line()}, current sentence: {get_sentence(last_card)}")
    logger.info(f"use previous audio: {use_prev_audio}")
    if get_config().obs.get_game_from_scene:
        obs.update_current_game()
    if use_prev_audio:
        lines = get_utility_window().get_selected_lines()
        with util.lock:
            update_anki_card(last_card, note=get_initial_card_info(last_card, lines), reuse_audio=True)
        get_utility_window().reset_checkboxes()
    else:
        logger.info("New card(s) detected! Added to Processing Queue!")
        card_queue.append(last_card)
        replay = obs.save_replay_buffer()
        wait_for_stable_file(replay)
        process_replay(replay)


def monitor_anki():
    try:
        # Continuously check for new cards
        while True:
            check_for_new_cards()
            time.sleep(get_config().anki.polling_rate / 1000.0)  # Check every 200ms
    except KeyboardInterrupt:
        print("Stopped Checking For Anki Cards...")


def start_monitoring_anki():
    # Start monitoring anki
    if get_config().obs.enabled and get_config().features.full_auto:
        obs_thread = threading.Thread(target=monitor_anki)
        obs_thread.daemon = True  # Ensures the thread will exit when the main program exits
        obs_thread.start()

def get_screenshot():
    try:
        image = obs.get_screenshot()
        wait_for_stable_file(image, timeout=3)
        if not image:
            raise Exception("Failed to get Screenshot from OBS")
        encoded_image = ffmpeg.process_image(image)
        if get_config().anki.update_anki and get_config().screenshot.screenshot_hotkey_updates_anki:
            last_note = anki.get_last_anki_card()
            if last_note:
                logger.debug(json.dumps(last_note))
            if get_config().features.backfill_audio:
                last_note = anki.get_cards_by_sentence(gametext.current_line)
            if last_note:
                anki.add_image_to_card(last_note, encoded_image)
                notification.send_screenshot_updated(last_note.get_field(get_config().anki.word_field))
                if get_config().features.open_anki_edit:
                    notification.open_anki_card(last_note.noteId)
            else:
                notification.send_screenshot_saved(encoded_image)
        else:
            notification.send_screenshot_saved(encoded_image)
    except Exception as e:
        logger.error(f"Failed to get Screenshot {e}")


def create_image():
    """Create a simple pickaxe icon."""
    width, height = 64, 64
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))  # Transparent background
    draw = ImageDraw.Draw(image)

    # Handle (rectangle)
    handle_color = (139, 69, 19)  # Brown color
    draw.rectangle([(30, 15), (34, 50)], fill=handle_color)

    # Blade (triangle-like shape)
    blade_color = (192, 192, 192)  # Silver color
    draw.polygon([(15, 15), (49, 15), (32, 5)], fill=blade_color)

    return image


def open_settings():
    obs.update_current_game()
    settings_window.show()


def open_multimine():
    obs.update_current_game()
    get_utility_window().show()

def play_most_recent_audio():
    if get_config().advanced.audio_player_path or get_config().advanced.video_player_path and len(gametext.line_history.values) > 0:
        get_utility_window().line_for_audio = gametext.line_history.values[-1]
        obs.save_replay_buffer()
    else:
        logger.error("Feature Disabled. No audio or video player path set in config!")


def open_log():
    """Function to handle opening log."""
    """Open log file with the default application."""
    log_file_path = get_log_path()
    if not os.path.exists(log_file_path):
        logger.error("Log file not found!")
        return

    if sys.platform.startswith("win"):  # Windows
        os.startfile(log_file_path)
    elif sys.platform.startswith("darwin"):  # macOS
        subprocess.call(["open", log_file_path])
    elif sys.platform.startswith("linux"):  # Linux
        subprocess.call(["xdg-open", log_file_path])
    else:
        logger.error("Unsupported platform!")
    logger.info("Log opened.")


def exit_program(passed_icon, item):
    """Exit the application."""
    if not passed_icon:
        passed_icon = icon
    logger.info("Exiting...")
    passed_icon.stop()
    cleanup()


def play_pause(icon, item):
    global obs_paused, menu
    obs.toggle_replay_buffer()
    update_icon()


def update_icon():
    global menu, icon
    # Recreate the menu with the updated button text
    profile_menu = Menu(
        *[MenuItem(("Active: " if profile == get_master_config().current_profile else "") + profile, switch_profile) for
          profile in
          get_master_config().get_all_profile_names()]
    )

    menu = Menu(
        MenuItem("Open Settings", open_settings),
        MenuItem("Open Multi-Mine GUI", open_multimine),
        MenuItem("Open Log", open_log),
        MenuItem("Toggle Replay Buffer", play_pause),
        MenuItem("Restart OBS", restart_obs),
        MenuItem("Switch Profile", profile_menu),
        MenuItem("Exit", exit_program)
    )

    icon.menu = menu
    icon.update_menu()


def switch_profile(icon, item):
    if "Active:" in item.text:
        logger.error("You cannot switch to the currently active profile!")
        return
    logger.info(f"Switching to profile: {item.text}")
    prev_config = get_config()
    get_master_config().current_profile = item.text
    switch_profile_and_save(item.text)
    settings_window.reload_settings()
    update_icon()
    if get_config().restart_required(prev_config):
        send_restart_signal()


def run_tray():
    global menu, icon

    profile_menu = Menu(
        *[MenuItem(("Active: " if profile == get_master_config().current_profile else "") + profile, switch_profile) for
          profile in
          get_master_config().get_all_profile_names()]
    )

    menu = Menu(
        MenuItem("Open Settings", open_settings),
        MenuItem("Open Multi-Mine GUI", open_multimine),
        MenuItem("Open Log", open_log),
        MenuItem("Toggle Replay Buffer", play_pause),
        MenuItem("Restart OBS", restart_obs),
        MenuItem("Switch Profile", profile_menu),
        MenuItem("Exit", exit_program)
    )

    icon = Icon("TrayApp", create_image(), "Game Sentence Miner", menu)
    icon.run()


# def close_obs():
#     if obs_process:
#         logger.info("Closing OBS")
#         proc = None
#         if obs_process:
#             try:
#                 logger.info("Closing OBS")
#                 proc = psutil.Process(obs_process)
#                 proc.send_signal(signal.CTRL_BREAK_EVENT)
#                 proc.wait(timeout=5)
#                 logger.info("Process closed gracefully.")
#             except psutil.NoSuchProcess:
#                 logger.info("PID already closed.")
#             except psutil.TimeoutExpired:
#                 logger.info("Process did not close gracefully, terminating.")
#                 proc.terminate()
#                 proc.wait()

def close_obs():
    if obs.obs_process:
        try:
            subprocess.run(["taskkill", "/PID", str(obs.obs_process.pid), "/F"], check=True, capture_output=True, text=True)
            print(f"OBS (PID {obs.obs_process.pid}) has been terminated.")
        except subprocess.CalledProcessError as e:
            print(f"Error terminating OBS: {e.stderr}")
    else:
        print("OBS is not running.")


def restart_obs():
    if obs.obs_process:
        close_obs()
        time.sleep(2)
        obs.start_obs()
        obs.connect_to_obs()


def cleanup():
    logger.info("Performing cleanup...")
    util.keep_running = False

    if get_config().obs.enabled:
        obs.stop_replay_buffer()
        obs.disconnect_from_obs()
    if get_config().obs.close_obs:
        close_obs()

    proc: Popen
    for proc in procs_to_close:
        try:
            logger.info(f"Terminating process {proc.args[0]}")
            proc.terminate()
            proc.wait()  # Wait for OBS to fully close
            logger.info(f"Process {proc.args[0]} terminated.")
        except psutil.NoSuchProcess:
            logger.info("PID already closed.")
        except Exception as e:
            proc.kill()
            logger.error(f"Error terminating process {proc}: {e}")

    if icon:
        icon.stop()

    settings_window.window.destroy()
    logger.info("Cleanup complete.")


def handle_exit():
    """Signal handler for graceful termination."""

    def _handle_exit(signum):
        logger.info(f"Received signal {signum}. Exiting gracefully...")
        cleanup()
        sys.exit(0)

    return _handle_exit

def initialize(reloading=False):
    global obs_process
    if not reloading:
        if is_windows():
            download_obs_if_needed()
            download_ffmpeg_if_needed()
        if get_config().obs.enabled:
            if get_config().obs.open_obs:
                obs_process = obs.start_obs()
            # obs.connect_to_obs(start_replay=True)
            # anki.start_monitoring_anki()
        # gametext.start_text_monitor()
        os.makedirs(get_config().paths.folder_to_watch, exist_ok=True)
        os.makedirs(get_config().paths.screenshot_destination, exist_ok=True)
        os.makedirs(get_config().paths.audio_destination, exist_ok=True)
    initial_checks()
    register_websocket_message_handler(handle_websocket_message)
    # if get_config().vad.do_vad_postprocessing:
    #     if VOSK in (get_config().vad.backup_vad_model, get_config().vad.selected_vad_model):
    #         vosk_helper.get_vosk_model()
    #     if WHISPER in (get_config().vad.backup_vad_model, get_config().vad.selected_vad_model):
    #         whisper_helper.initialize_whisper_model()

def initialize_async():
    tasks = [gametext.start_text_monitor, connect_websocket, run_tray]
    threads = []
    tasks.append(start_monitoring_anki)
    for task in tasks:
        threads.append(util.run_new_thread(task))
    return threads


def post_init():
    def do_post_init():
        global silero_trim, whisper_helper, vosk_helper
        logger.info("Post-Initialization started.")
        if get_config().obs.enabled:
            obs.connect_to_obs()
            check_obs_folder_is_correct()
            from GameSentenceMiner.vad import vosk_helper
            from GameSentenceMiner.vad import whisper_helper
            if get_config().vad.is_vosk():
                vosk_helper.get_vosk_model()
            if get_config().vad.is_whisper():
                whisper_helper.initialize_whisper_model()
            if get_config().vad.is_silero():
                pass

    util.run_new_thread(do_post_init)


def handle_websocket_message(message: Message):
    match message.function:
        case "quit":
            cleanup()
            sys.exit(0)
        case _:
            logger.debug(f"unknown message from electron websocket: {message.to_json()}")


def main(reloading=False):
    global root, settings_window
    logger.info("Script started.")
    root = ttk.Window(themename='darkly')
    settings_window = config_gui.ConfigApp(root)
    init_utility_window(root)
    initialize(reloading)
    initialize_async()
    # observer = Observer()
    # observer.schedule(VideoToAudioHandler(), get_config().paths.folder_to_watch, recursive=False)
    # observer.start()
    if not is_linux():
        register_hotkeys()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, handle_exit())  # Handle `kill` commands
    signal.signal(signal.SIGINT, handle_exit())  # Handle Ctrl+C
    if is_windows():
        win32api.SetConsoleCtrlHandler(handle_exit())

    try:
        if get_config().general.open_config_on_startup:
            root.after(0, settings_window.show)
        if get_config().general.open_multimine_on_startup:
            root.after(0, get_utility_window().show)
        root.after(0, post_init)
        settings_window.add_save_hook(update_icon)
        settings_window.on_exit = exit_program
        root.mainloop()
    except KeyboardInterrupt:
        cleanup()

    # try:
    #     observer.stop()
    #     observer.join()
    # except Exception as e:
    #     logger.error(f"Error stopping observer: {e}")


if __name__ == "__main__":
    logger.info("Starting GSM")
    main()

