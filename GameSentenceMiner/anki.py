import base64
import subprocess
import urllib.request
from requests import post

from GameSentenceMiner import util, notification, ffmpeg
from GameSentenceMiner.configuration import *
from GameSentenceMiner.configuration import get_config
from GameSentenceMiner.gametext import get_text_event
from GameSentenceMiner.model import AnkiCard
from GameSentenceMiner.obs import get_current_game
from GameSentenceMiner.util import remove_html_and_cloze_tags, combine_dialogue


audio_in_anki = None
screenshot_in_anki = None
prev_screenshot_in_anki = None

# Global variables to track state


def update_anki_card(last_note: AnkiCard, note=None, audio_path='', video_path='', tango='', reuse_audio=False,
                     should_update_audio=True, ss_time=0, game_line=None, selected_lines=None):
    global audio_in_anki, screenshot_in_anki, prev_screenshot_in_anki
    update_audio = should_update_audio and (get_config().anki.sentence_audio_field and not
    last_note.get_field(get_config().anki.sentence_audio_field) or get_config().anki.overwrite_audio)
    update_picture = (get_config().anki.picture_field and get_config().anki.overwrite_picture) or not \
    last_note.get_field(get_config().anki.picture_field)

    if not reuse_audio:
        if update_audio:
            audio_in_anki = store_media_file(audio_path)
        if update_picture:
            screenshot = ffmpeg.get_screenshot(video_path, ss_time)
            screenshot_in_anki = store_media_file(screenshot)
            if get_config().paths.remove_screenshot:
                os.remove(screenshot)
        if get_config().anki.previous_image_field:
            prev_screenshot = ffmpeg.get_screenshot(video_path, ffmpeg.get_screenshot_time(video_path, selected_lines[0].prev if selected_lines else game_line.prev))
            prev_screenshot_in_anki = store_media_file(prev_screenshot)
            if get_config().paths.remove_screenshot:
                os.remove(prev_screenshot)
    audio_html = f"[sound:{audio_in_anki}]"
    image_html = f"<img src=\"{screenshot_in_anki}\">"
    prev_screenshot_html = f"<img src=\"{prev_screenshot_in_anki}\">"

    # note = {'id': last_note.noteId, 'fields': {}}

    if update_audio:
        note['fields'][get_config().anki.sentence_audio_field] = audio_html

    if update_picture:
        note['fields'][get_config().anki.picture_field] = image_html

    if prev_screenshot_in_anki:
        note['fields'][get_config().anki.previous_image_field] = prev_screenshot_html

    if get_config().anki.anki_custom_fields:
        for key, value in get_config().anki.anki_custom_fields.items():
            note['fields'][key] = str(value)

    invoke("updateNoteFields", note=note)
    tags = []
    if get_config().anki.custom_tags:
        tags.extend(get_config().anki.custom_tags)
    if get_config().anki.add_game_tag:
        tags.append(get_current_game().replace(" ", ""))
    if tags:
        tag_string = " ".join(tags)
        invoke("addTags", tags=tag_string, notes=[last_note.noteId])
    logger.info(f"UPDATED ANKI CARD FOR {last_note.noteId}")
    if get_config().features.notify_on_update:
        notification.send_note_updated(tango)
    if get_config().features.open_anki_edit:
        notification.open_anki_card(last_note.noteId)

    if get_config().audio.external_tool:
        open_audio_in_external(f"{get_config().audio.anki_media_collection}/{audio_in_anki}")


def open_audio_in_external(fileabspath, shell=False):
    logger.info(f"Opening audio: {fileabspath} in external Program: {get_config().audio.external_tool}")
    if shell:
        subprocess.Popen(f' "{get_config().audio.external_tool}" "{fileabspath}" ', shell=True)
    else:
        subprocess.Popen([get_config().audio.external_tool, fileabspath])


def add_image_to_card(last_note: AnkiCard, image_path):
    global screenshot_in_anki
    update_picture = get_config().anki.overwrite_picture or not last_note.get_field(get_config().anki.picture_field)

    if update_picture:
        screenshot_in_anki = store_media_file(image_path)
        if get_config().paths.remove_screenshot:
            os.remove(image_path)

    image_html = f"<img src=\"{screenshot_in_anki}\">"

    note = {'id': last_note.noteId, 'fields': {}}

    if update_picture:
        note['fields'][get_config().anki.picture_field] = image_html

    invoke("updateNoteFields", note=note)

    logger.info(f"UPDATED IMAGE FOR ANKI CARD {last_note.noteId}")


def get_initial_card_info(last_note: AnkiCard, selected_lines):
    note = {'id': last_note.noteId, 'fields': {}}
    if not last_note:
        return note
    game_line = get_text_event(last_note)
    sentences = []
    sentences_text = ''
    if selected_lines:
        try:
            sentence_in_anki = last_note.get_field(get_config().anki.sentence_field)
            logger.info(f"Attempting Preserve HTML for multi-line")
            for line in selected_lines:
                if remove_html_and_cloze_tags(sentence_in_anki) in line.text:
                    sentences.append(sentence_in_anki)
                    logger.info("Found matching line in Anki, Preserving HTML!")
                else:
                    sentences.append(line.text)

            logger.debug(f"Attempting to Fix Character Dialogue Format")
            logger.debug([f"{line}" for line in sentences])
            try:
                combined_lines = combine_dialogue(sentences)
                logger.debug(combined_lines)
                if combined_lines:
                    sentences_text = "".join(combined_lines)
            except Exception as e:
                logger.debug(f'Error combining dialogue: {e}, defaulting')
                pass
        except Exception as e:
            logger.debug(f"Error preserving HTML for multi-line: {e}")
            pass
        multi_line_sentence = sentences_text if sentences_text else get_config().advanced.multi_line_line_break.join(sentences)
        if get_config().anki.multi_overwrites_sentence:
            note['fields'][get_config().anki.sentence_field] = multi_line_sentence
        else:
            logger.info(f"Configured to not overwrite sentence field, Multi-line Sentence If you want it, Note you need to do ctrl+shift+x in anki to paste properly:\n\n" + (sentences_text if sentences_text else get_config().advanced.multi_line_line_break.join(sentences)) + "\n")
        if get_config().advanced.multi_line_sentence_storage_field:
            note['fields'][get_config().advanced.multi_line_sentence_storage_field] = multi_line_sentence

    if get_config().anki.previous_sentence_field and game_line.prev and not \
            last_note.get_field(get_config().anki.previous_sentence_field):
        logger.debug(
            f"Adding Previous Sentence: {get_config().anki.previous_sentence_field and game_line.prev.text and not last_note.get_field(get_config().anki.previous_sentence_field)}")
        if selected_lines:
            note['fields'][get_config().anki.previous_sentence_field] = selected_lines[0].prev.text
        else:
            note['fields'][get_config().anki.previous_sentence_field] = game_line.prev.text
    return note


def store_media_file(path):
    return invoke('storeMediaFile', filename=path, data=convert_to_base64(path))


def convert_to_base64(file_path):
    with open(file_path, "rb") as file:
        file_base64 = base64.b64encode(file.read()).decode('utf-8')
    return file_base64


def request(action, **params):
    return {'action': action, 'params': params, 'version': 6}


def invoke(action, **params):
    request_json = json.dumps(request(action, **params)).encode('utf-8')
    # if action != "storeMediaFile":
    #     logger.debug(f"Hitting Anki. Action: {action}. Data: {request_json}")
    response = json.load(urllib.request.urlopen(urllib.request.Request(get_config().anki.url, request_json)))
    if len(response) != 2:
        raise Exception('response has an unexpected number of fields')
    if 'error' not in response:
        raise Exception('response is missing required error field')
    if 'result' not in response:
        raise Exception('response is missing required result field')
    if response['error'] is not None:
        raise Exception(response['error'])
    return response['result']


def get_last_anki_card() -> AnkiCard | dict:
    added_ids = invoke('findNotes', query='added:1')
    if not added_ids:
        return {}

    card_dict = invoke('notesInfo', notes=[added_ids[-1]])[0]
    try:
        return AnkiCard.from_dict(card_dict)
    except Exception as e:
        logger.error(f"Error fetching last card: {e}")
        logger.info(card_dict)
        raise e


def add_wildcards(expression):
    return '*' + '*'.join(expression) + '*'


def get_cards_by_sentence(sentence):
    sentence = sentence.replace(" ", "")
    query = f'{get_config().anki.sentence_audio_field}: {get_config().anki.sentence_field}:{add_wildcards(sentence)}'
    card_ids = invoke("findCards", query=query)

    if not card_ids:
        logger.warning(f"Didn't find any cards matching query:\n{query}")
        return {}
    if len(card_ids) > 1:
        logger.warning(f'Found more than 1, and not updating cards for query: \n{query}')
        return {}

    last_notes = invoke('notesInfo', notes=[card_ids[0]])[0]

    logger.info(f"Found Card to backfill!: {card_ids[0]}")

    return last_notes


# Check for new Anki cards and save replay buffer if detected


def sentence_is_same_as_previous(last_card):
    if not util.get_last_mined_line():
        return False
    return remove_html_and_cloze_tags(get_sentence(last_card)) == remove_html_and_cloze_tags(util.get_last_mined_line())

def get_sentence(card):
    return card.get_field(get_config().anki.sentence_field)

def check_tags_for_should_update(last_card):
    if get_config().anki.tags_to_check:
        found = False
        for tag in last_card.tags:
            if tag.lower() in get_config().anki.tags_to_check:
                found = True
                break
        if not found:
            logger.info(f"Card not tagged properly! Not updating! Note Tags: {last_card.tags}, Tags_To_Check {get_config().anki.tags_to_check}")
        return found
    else:
        return True


# Main function to handle the script lifecycle


# Fetch recent note IDs from Anki
def get_note_ids():
    response = post(get_config().anki.url, json={
        "action": "findNotes",
        "version": 6,
        "params": {"query": "added:1"}
    })
    result = response.json()
    return set(result['result'])


