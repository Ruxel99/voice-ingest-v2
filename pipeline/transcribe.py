
import os, pathlib
from openai import OpenAI

def transcribe_audio(path: pathlib.Path):
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("WHISPER_MODEL","whisper-1")
    with open(path, "rb") as f:
        resp = client.audio.transcriptions.create(model=model, file=f, language="es")
    text = getattr(resp, "text", "") or (resp.get("text","") if isinstance(resp, dict) else "")
    text = (text or "").strip()
    if not text:
        raise RuntimeError("Transcripción vacía o inválida")
    meta = {"engine":"openai-whisper","model":model,"lang":"es"}
    return text, meta
