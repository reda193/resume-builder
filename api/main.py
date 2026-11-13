from fastapi import FastAPI, HTTPException, UploadFile
from extract import extract_from_file, UnsupportedFileError, UnreadableFileError, AIExtractionError

app = FastAPI(title="Resume Builder API")

MAX_UPLOAD_BYTES = 5 * 1024 * 1024

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/extract")
def extract(file: UploadFile):
    data = file.file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The file is larger than 5 MB.")

    try:
        result = extract_from_file(data, file.filename)
    except UnsupportedFileError as e:
        raise HTTPException(status_code=415, detail=str(e))
    except UnreadableFileError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except AIExtractionError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return result

@app.post("/upload-test")
def upload_test(file: UploadFile):
    data = file.file.read()
    return {"filename": file.filename, "size": len(data)}