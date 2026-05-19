#!/bin/bash
BASE_DIR="$HOME/Music/DJ Music v3"
SP_ARCHIVE="$BASE_DIR/.spotify_download_archive.txt"
SC_ARCHIVE="$BASE_DIR/.sc_download_archive.txt"

# Ensure the global history database logs exist
touch "$SP_ARCHIVE"
touch "$SC_ARCHIVE"

clear
echo "=========================================="
echo "    MIRRORED CROSS-PLATFORM DOWNLOADER    "
echo "=========================================="
echo ""
read -p "Paste Spotify or SoundCloud Link and press Enter: " URL
echo ""
read -p "Enter subfolder name for this playlist: " FOLDER_NAME

TARGET_DIR="$BASE_DIR/$FOLDER_NAME"
mkdir -p "$TARGET_DIR"

if [[ "$URL" == *"spotify"* ]]; then
    echo ""
    echo "[INFO] Spotify mode activated (Using Global History Archive)..."
    cd "$TARGET_DIR"

    # FIX: Uses --archive pointing to a centralized global file across ALL folders
    spotdl download "$URL" --output "{title} - {artist}.{output-ext}" --client-id copypasteclientidhere --client-secret copypasteclientsecrethere --use-official-api --archive "$SP_ARCHIVE"

elif [[ "$URL" == *"soundcloud"* ]]; then
    echo ""
    echo "[INFO] SoundCloud mode activated (Using Global History Archive)..."
    cd "$TARGET_DIR"

    # SoundCloud mirrored operation query logic
    scdl -l "$URL" --path "$TARGET_DIR" --no-playlist-folder --download-archive "$SC_ARCHIVE" --name-format "{title} - {artist}"

else
    echo ""
    echo "=========================================="
    echo "[ERROR] Link not supported!"
    echo "This script only accepts Spotify or SoundCloud links."
    echo "=========================================="
fi

echo ""
echo "=========================================="
echo "[PROCESS FINISHED]"
echo "=========================================="

