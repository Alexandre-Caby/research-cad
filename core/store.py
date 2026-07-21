"""LanceDB multimodal data plane: vector search over project text/image embeddings."""
import argparse

import lancedb
import numpy as np
import pyarrow as pa

from core import config


class LanceStore:
    def __init__(self, lance_dir=None, text_dim=None, image_dim=None):
        self.lance_dir = lance_dir or config.LANCE_DIR
        self.text_dim = text_dim
        self.image_dim = image_dim
        self.table_name = "projects"

    def _schema(self, text_dim, image_dim) -> pa.Schema:
        return pa.schema([
            pa.field("project_id", pa.string()),
            pa.field("name", pa.string()),
            pa.field("source", pa.string()),
            pa.field("text", pa.string()),
            pa.field("text_vector", pa.list_(pa.float32(), text_dim)),
            pa.field("image_path", pa.string()),
            pa.field("image_vector", pa.list_(pa.float32(), image_dim)),
            pa.field("components", pa.list_(pa.string())),
            pa.field("yaml", pa.string()),
            pa.field("json", pa.string()),
            pa.field("board_metrics", pa.string()),
        ])

    def open_table(self):
        db = lancedb.connect(self.lance_dir)
        if self.table_name in db.table_names():
            return db.open_table(self.table_name)
        if self.text_dim is None or self.image_dim is None:
            raise RuntimeError(
                "cannot open table before dims are known: run upsert() first or pass text_dim/image_dim"
            )
        return db.create_table(self.table_name, schema=self._schema(self.text_dim, self.image_dim))

    def upsert(self, rows: list[dict]) -> None:
        if not rows:
            return
        if self.text_dim is None:
            self.text_dim = len(rows[0]["text_vector"])
        if self.image_dim is None:
            self.image_dim = len(rows[0]["image_vector"])

        table = self.open_table()
        data = [self._to_arrow_row(row) for row in rows]
        (
            table.merge_insert("project_id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(data)
        )

    def _to_arrow_row(self, row: dict) -> dict:
        out = dict(row)
        for key in ("text_vector", "image_vector"):
            if isinstance(out[key], np.ndarray):
                out[key] = out[key].tolist()
        return out

    def search(self, vector, column, k=10) -> list[dict]:
        table = self.open_table()
        return table.search(vector, vector_column_name=column).limit(k).to_list()

    def export_parquet(self, out_path) -> None:
        table = self.open_table()
        table.to_pandas().to_parquet(out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", required=True)
    args = parser.parse_args()
    LanceStore().export_parquet(args.export)
