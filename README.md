# Hackathon2026
link for the HackathonUI dashboard: [HackathonUI Dashboard](https://hackathon2026-adyjugjrkcyjovactvikuf.streamlit.app/)

To set up the data for this project:

Download the datasets from https://drive.google.com/drive/folders/11aoK0yXbuTYtckZeX4dcr6gOKYkqZSyE?usp=drive_link.

Extract the downloaded archive.

Move the uncompressed scraped_data and internal_data folders directly into the main project directory.

main_project_folder/
├── scraped_data/                 # Downloaded external data (uncompressed from Google Drive)
├── internal_data/                # Downloaded internal data (uncompressed from Google Drive)
├── models/                       # Auto-generated folder that holds the saved XGBoost model files[cite: 1]
└── checkpoints/                  # Auto-generated folder that holds per-client checkpoint JSON files for incremental updates[cite: 1]
