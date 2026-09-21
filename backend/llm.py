import os
from dotenv import load_dotenv
from google import genai

from backend.schemas import MemoryExtraction


# Load variables from .env
load_dotenv()

# Get Gemini API key. The client is created lazily so the server can boot
# (and serve everything except AI extraction) without credentials configured.
api_key = os.getenv("GEMINI_API_KEY")
client = None

def _get_client():
    """Return the Gemini client, creating it on first use."""
    global client
    if client is None:
        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to .env to enable AI extraction."
            )
        client = genai.Client(api_key=key)
    return client


# Models to try in order. The primary is the fastest; the fallbacks absorb
# transient capacity errors (e.g. 503 UNAVAILABLE) so a live demo never dies
# on one unlucky request.
MODEL_CHAIN = (
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
)


async def generate(prompt: str, config=None):
    """Generate content, falling back through MODEL_CHAIN on failure."""
    last_exc = None
    for model in MODEL_CHAIN:
        try:
            return await _get_client().aio.models.generate_content(
                model=model, contents=prompt, config=config
            )
        except Exception as e:  # noqa: BLE001 — try the next model in the chain
            last_exc = e
    raise last_exc


async def extract_memories(text: str) -> MemoryExtraction:

    prompt = f"""
You are the memory extraction system for ProjectBrain.

ProjectBrain stores important information about software projects.

Read the following project conversation and extract important
project memories.

Text:
{text}

Extract things such as:
- decisions
- experiments
- rejected approaches
- facts
- architecture choices
- issues
- goals

For each memory:
- type: choose the most appropriate category
- title: short descriptive title
- content: concise description of the important information
- status: active, superseded, rejected, completed, or archived
- entities: important technologies, concepts, or components mentioned

Only extract information that is actually present in the text.
Do not invent information.
"""

    # Async API keeps the FastAPI event loop free while Gemini is thinking
    response = await generate(
        prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": MemoryExtraction,
        },
    )

    return MemoryExtraction.model_validate_json(response.text)


if __name__ == "__main__":

    import asyncio

    test_text = (
        "We tried Tesseract for OCR but rejected it because "
        "handwriting recognition was poor. We will evaluate PaddleOCR instead."
    )

    result = asyncio.run(extract_memories(test_text))

    print(result.model_dump_json(indent=2))