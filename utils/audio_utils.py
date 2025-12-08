def is_supported_audio(path, exts):
    try:
        return path.suffix.lower() in exts and path.stat().st_size>0
    except Exception:
        return False
