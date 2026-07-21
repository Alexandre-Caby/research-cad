import numpy as np

from core import config


class TextEncoder:
    def __init__(self, model=config.TEXT_MODEL, device=None):
        self.model_name = model
        self.device = device or config.device()
        self._model = None
        self._dim = None

    @property
    def dim(self):
        # callers (e.g. zero-vector fill) may need dim before any text is ever encoded
        return self._dim if self._dim is not None else config.TEXT_DIM

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device=self.device)
            self._dim = self._model.get_sentence_embedding_dimension()

    def encode(self, texts: list[str], batch_size=config.EMBED_BATCH) -> np.ndarray:
        self._load()
        return self._model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        ).astype("float32")


class ImageEncoder:
    def __init__(self, model=config.IMAGE_MODEL, device=None):
        self.model_name = model
        self.device = device or config.device()
        self._model = None
        self._processor = None
        self._dim = None

    @property
    def dim(self):
        # batches with zero images never call _load(), so dim must still resolve
        return self._dim if self._dim is not None else config.IMAGE_DIM

    def _load(self):
        if self._model is None:
            from transformers import AutoModel, AutoProcessor

            self._model = AutoModel.from_pretrained(self.model_name).to(self.device)
            self._processor = AutoProcessor.from_pretrained(self.model_name)
            self._dim = self._model.config.text_config.hidden_size

    @staticmethod
    def _normalize(vecs: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return (vecs / norms).astype("float32")

    def encode_images(self, paths: list[str], batch_size=config.IMAGE_BATCH) -> np.ndarray:
        import torch
        from PIL import Image, UnidentifiedImageError

        self._load()
        vectors = np.zeros((len(paths), self.dim), dtype="float32")
        for start in range(0, len(paths), batch_size):
            chunk = paths[start:start + batch_size]
            images, offsets = [], []
            for offset, p in enumerate(chunk):
                try:
                    images.append(Image.open(p).convert("RGB"))
                    offsets.append(offset)
                except (FileNotFoundError, UnidentifiedImageError, OSError):
                    continue  # a decode failure must not sink the rest of the batch
            if not images:
                continue
            inputs = self._processor(images=images, return_tensors="pt").to(self.device)
            with torch.no_grad():
                features = self._model.get_image_features(**inputs)
            for offset, vec in zip(offsets, self._normalize(features.cpu().numpy())):
                vectors[start + offset] = vec
        return vectors

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        import torch

        self._load()
        inputs = self._processor(
            text=texts, return_tensors="pt", padding="max_length"
        ).to(self.device)
        with torch.no_grad():
            features = self._model.get_text_features(**inputs)
        return self._normalize(features.cpu().numpy())
