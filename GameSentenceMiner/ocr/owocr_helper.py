import asyncio
import ctypes
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox

import mss
import websockets
from rapidfuzz import fuzz

from GameSentenceMiner import obs, util
from GameSentenceMiner.configuration import get_config, get_app_directory, get_temporary_directory
from GameSentenceMiner.electron_config import get_ocr_scan_rate, get_requires_open_window
from GameSentenceMiner.ocr.gsm_ocr_config import OCRConfig, Rectangle
from GameSentenceMiner.owocr.owocr import screen_coordinate_picker, run
from GameSentenceMiner.owocr.owocr.run import TextFiltering
from GameSentenceMiner.util import do_text_replacements, OCR_REPLACEMENTS_FILE

CONFIG_FILE = Path("ocr_config.json")
DEFAULT_IMAGE_PATH = r"C:\Users\Beangate\Pictures\msedge_acbl8GL7Ax.jpg"  # CHANGE THIS
logger = logging.getLogger("GSM_OCR")
logger.setLevel(logging.DEBUG)
# Create a file handler for logging
log_file = os.path.join(get_app_directory(), "logs", "ocr_log.txt")
os.makedirs(os.path.join(get_app_directory(), "logs"), exist_ok=True)
file_handler = RotatingFileHandler(log_file, maxBytes=1024 * 1024, backupCount=5, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
# Create a formatter and set it for the handler
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
# Add the handler to the logger
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)


def get_new_game_cords():
    """Allows multiple coordinate selections."""
    coords_list = []
    with mss.mss() as sct:
        monitors = sct.monitors
        monitor_map = {i: mon for i, mon in enumerate(monitors)}
        while True:
            selected_monitor_index, cords = screen_coordinate_picker.get_screen_selection_with_monitor(monitor_map)
            selected_monitor = monitor_map[selected_monitor_index]
            coords_list.append({"monitor": {"left": selected_monitor["left"], "top": selected_monitor["top"],
                                            "width": selected_monitor["width"], "height": selected_monitor["height"],
                                            "index": selected_monitor_index}, "coordinates": cords,
                                "is_excluded": False})
            if messagebox.askyesno("Add Another Region", "Do you want to add another region?"):
                continue
            else:
                break
    app_dir = Path.home() / "AppData" / "Roaming" / "GameSentenceMiner"
    ocr_config_dir = app_dir / "ocr_config"
    ocr_config_dir.mkdir(parents=True, exist_ok=True)
    obs.connect_to_obs()
    scene = util.sanitize_filename(obs.get_current_scene())
    config_path = ocr_config_dir / f"{scene}.json"
    with open(config_path, 'w') as f:
        json.dump({"scene": scene, "window": None, "rectangles": coords_list}, f, indent=4)
    print(f"Saved OCR config to {config_path}")
    return coords_list


def get_ocr_config() -> OCRConfig:
    """Loads and updates screen capture areas from the corresponding JSON file."""
    app_dir = Path.home() / "AppData" / "Roaming" / "GameSentenceMiner"
    ocr_config_dir = app_dir / "ocr_config"
    os.makedirs(ocr_config_dir, exist_ok=True)
    obs.connect_to_obs()
    scene = util.sanitize_filename(obs.get_current_scene())
    config_path = ocr_config_dir / f"{scene}.json"
    if not config_path.exists():
        config_path.touch()
        return
    try:
        with open(config_path, 'r', encoding="utf-8") as f:
            config_data = json.load(f)
        if "rectangles" in config_data and isinstance(config_data["rectangles"], list) and all(
                isinstance(item, list) and len(item) == 4 for item in config_data["rectangles"]):
            # Old config format, convert to new
            new_rectangles = []
            with mss.mss() as sct:
                monitors = sct.monitors
                default_monitor = monitors[1] if len(monitors) > 1 else monitors[0]
                for rect in config_data["rectangles"]:
                    new_rectangles.append({
                        "monitor": {
                            "left": default_monitor["left"],
                            "top": default_monitor["top"],
                            "width": default_monitor["width"],
                            "height": default_monitor["height"],
                            "index": 0  # Assuming single monitor for old config
                        },
                        "coordinates": rect,
                        "is_excluded": False
                    })
                if 'excluded_rectangles' in config_data:
                    for rect in config_data['excluded_rectangles']:
                        new_rectangles.append({
                            "monitor": {
                                "left": default_monitor["left"],
                                "top": default_monitor["top"],
                                "width": default_monitor["width"],
                                "height": default_monitor["height"],
                                "index": 0  # Assuming single monitor for old config
                            },
                            "coordinates": rect,
                            "is_excluded": True
                        })
            new_config_data = {"scene": config_data.get("scene", scene), "window": config_data.get("window", None),
                               "rectangles": new_rectangles, "coordinate_system": "absolute"}
            with open(config_path, 'w', encoding="utf-8") as f:
                json.dump(new_config_data, f, indent=4)
            return OCRConfig.from_dict(new_config_data)
        elif "rectangles" in config_data and isinstance(config_data["rectangles"], list) and all(
                isinstance(item, dict) and "coordinates" in item for item in config_data["rectangles"]):
            return OCRConfig.from_dict(config_data)
        else:
            raise Exception(f"Invalid config format in {config_path}.")
    except json.JSONDecodeError:
        print("Error decoding JSON. Please check your config file.")
        return None
    except Exception as e:
        print(f"Error loading config: {e}")
        return None


websocket_server_thread = None
websocket_queue = queue.Queue()
paused = False


class WebsocketServerThread(threading.Thread):
    def __init__(self, read):
        super().__init__(daemon=True)
        self._loop = None
        self.read = read
        self.clients = set()
        self._event = threading.Event()

    @property
    def loop(self):
        self._event.wait()
        return self._loop

    async def send_text_coroutine(self, message):
        for client in self.clients:
            await client.send(message)

    async def server_handler(self, websocket):
        self.clients.add(websocket)
        try:
            async for message in websocket:
                if self.read and not paused:
                    websocket_queue.put(message)
                    try:
                        await websocket.send('True')
                    except websockets.exceptions.ConnectionClosedOK:
                        pass
                else:
                    try:
                        await websocket.send('False')
                    except websockets.exceptions.ConnectionClosedOK:
                        pass
        except websockets.exceptions.ConnectionClosedError:
            pass
        finally:
            self.clients.remove(websocket)

    def send_text(self, text, line_time: datetime):
        if text:
            return asyncio.run_coroutine_threadsafe(
                self.send_text_coroutine(json.dumps({"sentence": text, "time": line_time.isoformat()})), self.loop)

    def stop_server(self):
        self.loop.call_soon_threadsafe(self._stop_event.set)

    def run(self):
        async def main():
            self._loop = asyncio.get_running_loop()
            self._stop_event = stop_event = asyncio.Event()
            self._event.set()
            self.server = start_server = websockets.serve(self.server_handler,
                                                          get_config().general.websocket_uri.split(":")[0],
                                                          get_config().general.websocket_uri.split(":")[1],
                                                          max_size=1000000000)
            async with start_server:
                await stop_event.wait()

        asyncio.run(main())


all_cords = None
rectangles = None

def do_second_ocr(ocr1_text, rectangle_index, time, img):
    global twopassocr, ocr2, last_ocr1_results, last_ocr2_results
    try:
        orig_text, text = run.process_and_write_results(img, None, None, None, None,
                                                        engine=ocr2)
        previous_ocr2_text = last_ocr2_results[rectangle_index]
        if fuzz.ratio(previous_ocr2_text, text) >= 80:
            logger.info("Seems like the same text from previous ocr2 result, not sending")
            return
        img.save(os.path.join(get_temporary_directory(), "last_successful_ocr.png"))
        last_ocr2_results[rectangle_index] = text
        send_result(text, time)
        img.close()
    except json.JSONDecodeError:
        print("Invalid JSON received.")
    except Exception as e:
        logger.exception(e)
        print(f"Error processing message: {e}")

def send_result(text, time):
    if text:
        text = do_text_replacements(text, OCR_REPLACEMENTS_FILE)
        if get_config().advanced.ocr_sends_to_clipboard:
            import pyperclip
            pyperclip.copy(text)
        websocket_server_thread.send_text(text, time)


last_oneocr_results_to_check = {}  # Store last OCR result for each rectangle
last_oneocr_times = {}    # Store last OCR time for each rectangle
text_stable_start_times = {} # Store the start time when text becomes stable for each rectangle
previous_imgs = {}
orig_text_results = {} # Store original text results for each rectangle
TEXT_APPEARENCE_DELAY = get_ocr_scan_rate() * 1000 + 500  # Adjust as needed

def text_callback(text, orig_text, rectangle_index, time, img=None):
    global twopassocr, ocr2, last_oneocr_results_to_check, last_oneocr_times, text_stable_start_times, orig_text_results
    orig_text_string = ''.join([item for item in orig_text if item is not None]) if orig_text else ""
    # logger.debug(orig_text_string)

    current_time = time if time else datetime.now()

    previous_text = last_oneocr_results_to_check.pop(rectangle_index, "").strip()
    previous_orig_text = orig_text_results.get(rectangle_index, "").strip()

    # print(previous_orig_text)
    # if orig_text:
    #     print(orig_text_string)
    if not twopassocr:
        if previous_orig_text and fuzz.ratio(orig_text_string, previous_orig_text) >= 80:
            logger.info("Seems like Text we already sent, not doing anything.")
            return
        img.save(os.path.join(get_temporary_directory(), "last_successful_ocr.png"))
        send_result(text, time)
        orig_text_results[rectangle_index] = orig_text_string
        last_ocr1_results[rectangle_index] = previous_text
    if not text:
        if previous_text:
            if rectangle_index in text_stable_start_times:
                stable_time = text_stable_start_times.pop(rectangle_index)
                previous_img = previous_imgs.pop(rectangle_index)
                previous_result = last_ocr1_results[rectangle_index]
                if previous_result and fuzz.ratio(previous_result, previous_text) >= 80:
                    logger.info("Seems like the same text, not " + "doing second OCR" if twopassocr else "sending")
                    return
                if previous_orig_text and fuzz.ratio(orig_text_string, previous_orig_text) >= 80:
                    logger.info("Seems like Text we already sent, not doing anything.")
                    return
                orig_text_results[rectangle_index] = orig_text_string
                last_ocr1_results[rectangle_index] = previous_text
                do_second_ocr(previous_text, rectangle_index, stable_time, previous_img)
            return
        return

    if rectangle_index not in last_oneocr_results_to_check:
        last_oneocr_results_to_check[rectangle_index] = text
        last_oneocr_times[rectangle_index] = current_time
        text_stable_start_times[rectangle_index] = current_time
        previous_imgs[rectangle_index] = img
        return

    stable = text_stable_start_times.get(rectangle_index)

    if stable:
        time_since_stable_ms = int((current_time - stable).total_seconds() * 1000)

        if time_since_stable_ms >= TEXT_APPEARENCE_DELAY:
            last_oneocr_results_to_check[rectangle_index] = text
            last_oneocr_times[rectangle_index] = current_time
    else:
        last_oneocr_results_to_check[rectangle_index] = text
        last_oneocr_times[rectangle_index] = current_time
    previous_imgs[rectangle_index] = img

done = False


def run_oneocr(ocr_config: OCRConfig, i, area=False):
    global done
    screen_area = None
    if ocr_config.rectangles:
        rect_config = ocr_config.rectangles[i]
        coords = rect_config.coordinates
        monitor_config = rect_config.monitor
        screen_area = ",".join(str(c) for c in coords) if area else None
    exclusions = list(rect.coordinates for rect in list(filter(lambda x: x.is_excluded, ocr_config.rectangles)))
    run.run(read_from="screencapture", write_to="callback",
            screen_capture_area=screen_area,
            # screen_capture_monitor=monitor_config['index'],
            screen_capture_window=ocr_config.window,
            screen_capture_only_active_windows=get_requires_open_window(),
            screen_capture_delay_secs=get_ocr_scan_rate(), engine=ocr1,
            text_callback=text_callback,
            screen_capture_exclusions=exclusions,
            rectangle=i,
            language=language)
    done = True


def get_window(window_name):
    import pygetwindow as gw
    try:
        windows = gw.getWindowsWithTitle(window_name)
        if windows:
            if len(windows) > 1:
                print(f"Warning: Multiple windows found with title '{window_name}'. Using the first one.")
            return windows[0]
        else:
            return None
    except Exception as e:
        print(f"Error finding window '{window_name}': {e}")
        return None

if __name__ == "__main__":
    global ocr1, ocr2, twopassocr, language
    import sys

    args = sys.argv[1:]
    if len(args) == 4:
        language = args[0]
        ocr1 = args[1]
        ocr2 = args[2]
        twopassocr = bool(int(args[3]))
    elif len(args) == 3:
        language = args[0]
        ocr1 = args[1]
        ocr2 = args[2]
        twopassocr = True
    elif len(args) == 2:
        language = args[0]
        ocr1 = args[1]
        ocr2 = None
        twopassocr = False
    else:
        language = "ja"
        ocr1 = "oneocr"
        ocr2 = "glens"
        twopassocr = True
    logger.info(f"Received arguments: ocr1={ocr1}, ocr2={ocr2}, twopassocr={twopassocr}")
    global ocr_config
    ocr_config: OCRConfig = get_ocr_config()
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    if ocr_config:
        if ocr_config.window:
            start_time = time.time()
            while time.time() - start_time < 30:
                if get_window(ocr_config.window):
                    break
                logger.info(f"Window: {ocr_config.window} Could not be found, retrying in 1 second...")
                time.sleep(1)
            else:
                logger.error(f"Window '{ocr_config.window}' not found within 30 seconds.")
                sys.exit(1)
    logger.info(f"Starting OCR with configuration: Window: {ocr_config.window}, Rectangles: {ocr_config.rectangles}, Engine 1: {ocr1}, Engine 2: {ocr2}, Two-pass OCR: {twopassocr}")
    if ocr_config:
        rectangles = list(filter(lambda rect: not rect.is_excluded, ocr_config.rectangles))
        last_ocr1_results = [""] * len(rectangles) if rectangles else [""]
        last_ocr2_results = [""] * len(rectangles) if rectangles else [""]
        oneocr_threads = []
        run.init_config(False)
        if rectangles:
            for i, rectangle in enumerate(rectangles):
                thread = threading.Thread(target=run_oneocr, args=(ocr_config, i,True, ), daemon=True)
                oneocr_threads.append(thread)
                thread.start()
        else:
            single_ocr_thread = threading.Thread(target=run_oneocr, args=(ocr_config, 0,False, ), daemon=True)
            oneocr_threads.append(single_ocr_thread)
            single_ocr_thread.start()
        websocket_server_thread = WebsocketServerThread(read=True)
        websocket_server_thread.start()
        try:
            while not done:
                time.sleep(1)
        except KeyboardInterrupt as e:
            pass
        for thread in oneocr_threads:
            thread.join()
        # asyncio.run(websocket_client())
    else:
        print("Failed to load OCR configuration. Please check the logs.")
