import os

_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
STORAGE_DIR = os.path.join(_REPO_ROOT, "storage")
RAW_DIR = os.path.join(STORAGE_DIR, "1_raw_data")
REGISTRY_DIR = os.path.join(STORAGE_DIR, "2_register_data")
USABLE_DIR = os.path.join(STORAGE_DIR, "3_exploitable_data")
EXTRACTED_DIR = os.path.join(STORAGE_DIR, "4_extracted_data")
VECTOR_DIR = os.path.join(STORAGE_DIR, "5_vector_data")

def ensure_dirs_exist() -> None:
    for dir_path in [
        STORAGE_DIR,
        RAW_DIR,
        REGISTRY_DIR,
        USABLE_DIR,
        EXTRACTED_DIR,
        VECTOR_DIR,
    ]:
        os.makedirs(dir_path, exist_ok=True)

ensure_dirs_exist()

DB_PATH = os.path.join(REGISTRY_DIR, "cad_registry.db")
LANCE_DIR = os.path.join(VECTOR_DIR, "lance")

CAD_EXTENSIONS = {".sch", ".brd", ".kicad_sch", ".kicad_pcb", ".schdoc", ".pcbdoc"}

HF_DATASET_ID = "bshada/open-schematics"
HF_REVISION = "5c74c3648d22e73426bbeb4691397d9cc8053e34"

TEXT_MODEL = "BAAI/bge-m3"
TEXT_DIM = 1024
IMAGE_MODEL = "google/siglip2-so400m-patch16-512"
IMAGE_DIM = 1152 
EMBED_BATCH = int(os.environ.get("EMBED_BATCH", "64"))
IMAGE_BATCH = int(os.environ.get("IMAGE_BATCH", "32"))

HF_TOKEN = os.environ.get("HF_TOKEN", "")


def device() -> str:
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"