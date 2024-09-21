import threading
import time

import requests
from obswebsocket import obsws, requests as obs_requests

import anki
import config_reader
import util
from config_reader import *

# Global variables to track state
previous_note_ids = set()
first_run = True
obs_ws: obsws


def connect_to_obs():
    global obs_ws
    # Connect to OBS WebSocket
    if obs_enabled:
        try:
            obs_ws = obsws(OBS_HOST, OBS_PORT, OBS_PASSWORD)
            obs_ws.connect()
            print("Connected to OBS WebSocket.")
        except Exception as conn_exception:
            print(f"Error connecting to OBS WebSocket: {conn_exception}")


# Disconnect from OBS WebSocket
def disconnect_from_obs():
    if obs_ws:
        obs_ws.disconnect()
        logger.debug("Disconnected from OBS WebSocket.")


# Start replay buffer
def start_replay_buffer():
    try:
        info = obs_ws.call(obs_requests.StartReplayBuffer())
        logger.info("Replay buffer started.")
    except Exception as e:
        print(f"Error starting replay buffer: {e}")


# Stop replay buffer
def stop_replay_buffer():
    try:
        obs_ws.call(obs_requests.StopReplayBuffer())
        logger.info("Replay buffer stopped.")
    except Exception as e:
        print(f"Error stopping replay buffer: {e}")


# Fetch recent note IDs from Anki
def get_note_ids():
    try:
        response = requests.post(anki_url, json={
            "action": "findNotes",
            "version": 6,
            "params": {"query": "added:1"}
        })
        result = response.json()
        return set(result['result'])
    except Exception as e:
        print(f"Error fetching Anki notes: {e}")
        return set()


# Save the current replay buffer
def save_replay_buffer():
    try:
        response = obs_ws.call(obs_requests.SaveReplayBuffer())
        if response.status:
            print("Replay buffer saved successfully.")
        else:
            print("Failed to save replay buffer.")
    except Exception as e:
        print(f"Error saving replay buffer: {e}")


# Check for new Anki cards and save replay buffer if detected
def check_for_new_cards():
    global previous_note_ids, first_run
    current_note_ids = get_note_ids()
    new_card_ids = current_note_ids - previous_note_ids
    if new_card_ids and not first_run:
        update_new_card()
    first_run = False
    previous_note_ids = current_note_ids  # Update the list of known notes


def update_new_card():
    last_card = anki.get_last_anki_card()
    use_prev_audio = util.use_previous_audio
    if util.lock.locked():
        print("Audio still being Trimmed, Card Queued!")
        use_prev_audio = True
    with util.lock:
        print(f"use previous audio: {use_prev_audio}")
        if config_reader.get_game_from_scene:
            config_reader.current_game = get_game_from_scene()
        if use_prev_audio:
            anki.update_anki_card(last_card, reuse_audio=True)
        else:
            print("New card(s) detected!")
            save_replay_buffer()


# Main function to handle the script lifecycle
def monitor_anki():
    try:
        # Continuously check for new cards
        while True:
            check_for_new_cards()
            time.sleep(anki_polling_rate / 1000.0)  # Check every 200ms
    except KeyboardInterrupt:
        print("Stopped Checking For Anki Cards...")


def get_game_from_scene():
    try:
        response = obs_ws.call(obs_requests.GetCurrentProgramScene())
        data = response.datain
        return data.get('sceneName')
    except Exception as e:
        print(f"Couldn't get scene: {e}")


def start_monitoring_anki():
    # Start monitoring anki
    if obs_enabled and obs_full_auto_mode:
        obs_thread = threading.Thread(target=monitor_anki)
        obs_thread.daemon = True  # Ensures the thread will exit when the main program exits
        obs_thread.start()
