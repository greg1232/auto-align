
import os
import io

import srt

from smart_open import open
from pydub import AudioSegment
from google.cloud import speech

from alignment.sequence import Sequence
from alignment.vocabulary import Vocabulary
from alignment.sequencealigner import SimpleScoring, GlobalSequenceAligner

from auto_align.util.config import setup_config
from auto_align.util.config import setup_logging

from datetime import timedelta

import logging

logger = logging.getLogger(__name__)

def fix(arguments):
    config = setup_config(arguments)
    setup_logging(config)

    data = load_data(config)

    utterances = extract_utterances(data, config)

    save_utterances(utterances, config)

def load_data(config):
    audio = AudioSegment.from_file(config["data"]["audio_path"], "flac")

    with open(config["data"]["srt_path"]) as srt_file:
        for subtitle in srt.parse(srt_file.read()):
            start = max(0, int(subtitle.start.total_seconds() * 1000) - config["data"]["padding_ms"])
            end   = min(int(subtitle.end.total_seconds() * 1000) + config["data"]["padding_ms"], len(audio))
            yield { "audio" : audio, "start" : start, "end" : end, "max_length" : len(audio), "label" : subtitle.content }

def extract_utterances(data, config):
    model = GoogleSpeechAPIClient(config)

    for index, caption in enumerate(data):

        match = get_match(index, caption, model)

        if match is None:
            match = try_relaxed_match(index, caption, model)

        if match is None:
            continue

        logger.debug("Best match: " + str(match))

        if match["confidence"] < config["align"]["minimum_confidence"]:
            continue

        yield match

def try_relaxed_match(index, caption, model):
    relaxed_caption = dict(caption)

    center_time = relaxed_caption["start"] + ((relaxed_caption["end"] - relaxed_caption["start"]) / 2)

    relaxed_caption["start"] = max(0, center_time - 7400)
    relaxed_caption["end"] = min(relaxed_caption["max_length"], center_time + 7400)
    logger.debug("Trying relaxed match for: " + str(relaxed_caption))

    return get_match(index, relaxed_caption, model)

def get_match(index, caption, model):

    start = caption["start"]
    end = caption["end"]

    audio_segment = caption["audio"][start:end]

    name = str(index) + ".flac"

    result = model.predict(audio_segment, name, caption["label"])

    match = compare_captions(result, caption)

    logger.debug("Label is: " + str(caption["label"]))

    return match

class GoogleSpeechAPIClient:
    def __init__(self, config):
        self.config = config
        self.client = speech.SpeechClient()

    def predict(self, audio, name, label):

        logger.debug("Running google speech to text on: " + name)

        gcs_path = self.copy_to_gcs(audio, name)

        audio = speech.RecognitionAudio(uri=gcs_path)

        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.FLAC,
            sample_rate_hertz=48000,
            enable_word_time_offsets=True,
            speech_contexts = [speech.SpeechContext(phrases=get_label_words(label))
            ],
            #enable_word_confidence=True,
            language_code=self.config["deploy"]["model"]["language"],
        )

        response = self.client.recognize(config=config, audio=audio, timeout=15.0)

        logger.debug(" result is: " + str(response.results))

        return response.results

    def copy_to_gcs(self, audio, name):
        gcs_path = os.path.join(self.config["deploy"]["model"]["google_cloud_storage_path"], name)

        with open(gcs_path, "wb") as gcs_file:
            with io.BytesIO() as temp_file:
                audio.export(temp_file, format="flac")
                gcs_file.write(temp_file.read())

        return gcs_path

def compare_captions(results, caption):
    if len(results) == 0:
        return None

    label_words = get_label_words(caption["label"])

    if len(label_words) == 0:
        return None

    start = caption["start"]

    best_match = None
    old_confidence = 0

    for result in results:
        for alternative in result.alternatives:
            alignment = align(label_words, alternative)

            if alignment["confidence"] > old_confidence:

                old_confidence = alignment["confidence"]
                best_match = {
                    "confidence" : alignment["confidence"],
                    "start" : start + alignment["start_time"],
                    "end" : start + alignment["end_time"],
                    "label" : caption["label"]
                }

    return best_match

def get_label_words(label):
    words = label.split()

    return [word for word in words if not is_punctuation(word)]

def is_punctuation(word):
    return word == "." or word == ","

def align(label_words, alternative):
    label = Sequence(label_words)
    predicted = Sequence([word.word.lower() for word in alternative.words])

    # Create a vocabulary and encode the sequences.
    v = Vocabulary()
    predicted_encoded = v.encodeSequence(predicted)
    label_encoded = v.encodeSequence(label)

    logger.debug("Predicted encoded: " + str(predicted_encoded))
    logger.debug("Labeled encoded: " + str(label_encoded))

    # Create a scoring and align the sequences using global aligner.
    scoring = SimpleScoring(2, -1)
    aligner = GlobalSequenceAligner(scoring, -2)
    score, encodeds = aligner.align(label_encoded, predicted_encoded, backtrace=True)

    best_encoded = [item[1] for item in encodeds[0]]

    logger.debug("All alignments: " + str(encodeds[0]))
    logger.debug("Best encoded alignment: " + str(best_encoded))

    alignment_result = v.decodeSequenceAlignment(encodeds[0])
    confidence = alternative.confidence * (alignment_result.percentIdentity())

    start_time, end_time, confidence = find_start_and_end(best_encoded, alternative.words, confidence, vocab=v)

    result = {
        "start_time" : start_time,
        "end_time" : end_time,
        "confidence" : confidence
    }

    logger.debug("Result: " + str(result))

    return result

def find_start_and_end(best_encoded, words, confidence, vocab):
    match_begin = None
    match_end = None
    offset = 0

    if len(best_encoded) < 2:
        return 0,0,0

    word_index = 0
    while word_index < len(words):
        word = words[word_index]
        encoded_word = vocab.encode(word.word.lower())
        query_word = best_encoded[offset]

        logger.debug("Checking encoded word: " + str(encoded_word) + ", offset: " + str(offset) + ", searching for: " + str(query_word))

        if query_word == encoded_word:
            if offset == 0:
                match_begin = word
            if offset == (len(best_encoded) - 1):
                match_end = word
                break
            offset += 1
            word_index += 1
        elif query_word == 0:
            offset += 1
            continue
        else:
            offset = 0
            word_index += 1

    if match_begin is None or match_end is None:
        return 0,0,0

    return (match_begin.start_time.total_seconds() * 1000), (match_end.end_time.total_seconds() * 1000), confidence


def save_utterances(utterances, config):
    subtitles = []
    for index, utterance in enumerate(utterances):
        start_time = timedelta(milliseconds=utterance["start"])
        end_time = timedelta(milliseconds=utterance["end"])
        subtitles.append(srt.Subtitle(index=index, start=start_time, end=end_time, content=utterance["label"]))

    with open(config["data"]["result_srt_path"], "w") as output_file:
        output_file.write(srt.compose(subtitles))



