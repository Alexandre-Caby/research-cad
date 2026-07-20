#!/bin/bash
set -e

echo "--------------------------------------------------"
echo "🚀 ELECTRONIC CAD SCHEMATICS PIPELINE"
echo "--------------------------------------------------"

echo "LOG: Running ingestion (GitHub + Hugging Face + OSHWLab)..."
python3 core/ingest.py

echo "LOG: Running schematic processing (BOM extraction)..."
python3 core/clean_worker.py

echo "LOG: Running vectorisation (embedding generation)..."
python3 core/vectoriser.py

echo "LOG: Running the optional disk purge..."
read -p "Clear the temporary staging area (1_raw_data)? This cannot be undone. (yes/no): " confirm
if [[ "$confirm" == "yes" ]]; then
    python3 core/purge_worker.py --yes
fi

echo "--------------------------------------------------"
echo "🏁 PIPELINE RUN COMPLETE"
echo "--------------------------------------------------"