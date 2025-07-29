from google.cloud import speech_v1p1beta1 as speech
from google.oauth2 import service_account
import os

# Set credentials
CREDENTIALS_PATH = "google-speech-to-text-text-to-speech.json"
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = CREDENTIALS_PATH
credentials = service_account.Credentials.from_service_account_file(CREDENTIALS_PATH)
client = speech.SpeechClient(credentials=credentials)

# 🔧 Build configuration for Google Cloud's streaming speech recognition
def build_streaming_config():
    return speech.StreamingRecognitionConfig(
        config=speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="en-US",
            enable_automatic_punctuation=True
        ),
        interim_results=False,
        single_utterance=True
    )


# 🎙️ Transcribes streaming audio bytes using Google Cloud STT (Streaming)
def transcribe_streaming(audio_bytes: bytes) -> str:
    # 🧹 Clean leading null bytes (can cause decoding issues)
    while audio_bytes.startswith(b'\x00\x00'):
        audio_bytes = audio_bytes[2:]
        
     # 📤 Generator that yields small audio chunks for streaming    
    def request_gen():
        chunk_size = 4096
        for i in range(0, len(audio_bytes), chunk_size):
            yield speech.StreamingRecognizeRequest(audio_content=audio_bytes[i:i + chunk_size])

    try:
        responses = client.streaming_recognize(build_streaming_config(), request_gen())

        for response in responses:
            for result in response.results:
                if result.is_final and result.alternatives:
                    transcript = result.alternatives[0].transcript.strip()
                    print("🗣️ Google Streaming STT Final:", repr(transcript))
                    return transcript

        print("⚠️ Google Streaming STT gave no final result")

    except Exception as e:
        print("❌ Streaming STT error:", e)

    return ""
