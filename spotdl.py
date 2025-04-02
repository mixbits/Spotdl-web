#!/usr/bin/env python3
import os
import re
import subprocess
import zipfile
import json
import time
from flask import Flask, request, send_file, render_template, abort, redirect, jsonify
from spotipy.oauth2 import SpotifyOAuth
import spotipy
from mutagen.easyid3 import EasyID3
from dotenv import load_dotenv
from tqdm import tqdm
import threading
import uuid
import traceback

# Load .env file from the project directory
load_dotenv(dotenv_path="/volume1/web/spotdl/.env")

# Define base directory and HTML file path
BASE_DIR = "/volume1/web/spotdl"
HTML_FILE = os.path.join(BASE_DIR, "spotdl.html")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_APP_SECRET", "mydefaultsecret")

# Define the download directory
DOWNLOAD_DIR = "/volume1/web/spotdl/downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# Global progress tracking
PROGRESS_FILE = os.path.join(DOWNLOAD_DIR, "progress.json")

# Track downloads in progress
ACTIVE_DOWNLOADS = {}

def save_progress(data, download_id="default"):
    """Save progress data to a JSON file for polling"""
    progress_file = os.path.join(DOWNLOAD_DIR, f"progress_{download_id}.json")
    with open(progress_file, 'w') as f:
        json.dump(data, f)

def reset_progress(download_id="default"):
    """Reset progress data to initial state"""
    progress_data = {
        "total_files": 0,
        "current_file": 0,
        "current_file_progress": 0,
        "overall_percent": 0,
        "status": "idle",
        "error": None,
        "task_description": "",
        "timestamp": time.time(),
        "download_id": download_id,
        "failed_tracks": []
    }
    save_progress(progress_data, download_id)
    return progress_data

def update_progress(file_num=None, file_progress=None, total_files=None, status=None, error=None, task=None, force_percent=None, download_id="default", failed_track=None):
    """Update the progress state"""
    # Read current progress
    try:
        progress_file = os.path.join(DOWNLOAD_DIR, f"progress_{download_id}.json")
        with open(progress_file, 'r') as f:
            progress_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        progress_data = reset_progress(download_id)
    
    # Update fields if provided
    if file_num is not None:
        progress_data["current_file"] = file_num
    if file_progress is not None:
        progress_data["current_file_progress"] = file_progress
    if total_files is not None:
        progress_data["total_files"] = total_files
    if status is not None:
        progress_data["status"] = status
    if error is not None:
        progress_data["error"] = error
    if task is not None:
        progress_data["task_description"] = task
    if failed_track is not None:
        if "failed_tracks" not in progress_data:
            progress_data["failed_tracks"] = []
        progress_data["failed_tracks"].append(failed_track)
    
    # Calculate overall progress or use forced value
    if force_percent is not None:
        progress_data["overall_percent"] = force_percent
    elif progress_data["total_files"] > 0:
        file_weight = 1.0 / progress_data["total_files"]
        overall_progress = (progress_data["current_file"] - 1) * file_weight
        if progress_data["current_file"] > 0:  # If we're working on a file
            overall_progress += file_weight * progress_data["current_file_progress"]
        progress_data["overall_percent"] = int(overall_progress * 100)
    else:
        progress_data["overall_percent"] = 0
    
    # Update timestamp
    progress_data["timestamp"] = time.time()
    
    # Save to file
    save_progress(progress_data, download_id)
    return progress_data

def sanitize_filename(name):
    """Sanitize a string for use as a filename."""
    sanitized = re.sub(r'[^\w\s-]', '', name).strip()
    return sanitized.replace(' ', '_')

def create_spotify_client():
    client_id = os.environ.get("SPOTIPY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIPY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIPY_REDIRECT_URI", "http://[YOUR_IP]:7667/callback")
    
    if not client_id or not client_secret:
        abort(500, description="Spotify client credentials not set.")
    
    sp_oauth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope="user-library-read playlist-modify-public playlist-modify-private"
    )
    token_info = sp_oauth.get_cached_token()
    if not token_info:
        abort(401, description="Spotify token not found. Please visit /login to authorize your app.")
    token = token_info['access_token']
    return spotipy.Spotify(auth=token)

def filter_non_critical_errors(stderr):
    """Filter out non-critical errors and warnings from the stderr output"""
    non_critical_patterns = [
        "Python version 3.8 has been deprecated",
        "WARNING: [youtube]",
        "Signature extraction failed",
        "Some formats may be missing",
        "unable to obtain file audio codec with ffprobe",
        "Postprocessing: WARNING"
    ]
    
    # Split into lines to filter
    lines = stderr.splitlines()
    critical_lines = []
    
    for line in lines:
        is_critical = True
        for pattern in non_critical_patterns:
            if pattern in line:
                is_critical = False
                break
        if is_critical and line.strip() and "WARNING" not in line:
            critical_lines.append(line)
    
    return "\n".join(critical_lines) if critical_lines else None

def download_track(query, output_filename, file_num, total_files, download_id="default"):
    """
    Use yt-dlp to search YouTube for the track and download it as MP3.
    Checks that ffprobe is accessible.
    Updates progress periodically.
    """
    update_progress(file_num=file_num, total_files=total_files, 
                    task=f"Downloading track {file_num} of {total_files}: {os.path.basename(output_filename)}", 
                    download_id=download_id)
    
    ffprobe_cmd = os.environ.get("FFPROBE_PATH", "ffprobe")
    try:
        subprocess.run([ffprobe_cmd, "-version"], capture_output=True, text=True, check=True)
    except Exception as e:
        raise Exception("FFmpeg/ffprobe not installed or accessible. Set FFPROBE_PATH in your .env. Error: " + str(e))
    
    search_query = f"ytsearch1:{query} audio"
    
    # Create a process to run yt-dlp
    command = [
        "yt-dlp",
        search_query,
        "--extract-audio",
        "--audio-format", "mp3",
        "-o", output_filename
    ]
    
    # Set initial progress
    update_progress(file_progress=0, download_id=download_id)
    
    # Run the command
    process = subprocess.Popen(
        command, 
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # While process is running, increment progress
    simulated_progress = 0
    while process.poll() is None:
        # Increment by small amount each loop
        simulated_progress = min(simulated_progress + 0.05, 0.95)
        update_progress(file_progress=simulated_progress, download_id=download_id)
        time.sleep(0.5)
    
    # Get the result
    stdout, stderr = process.communicate()
    
    if process.returncode != 0:
        # Filter out non-critical errors and warnings
        critical_error = filter_non_critical_errors(stderr)
        
        if critical_error:
            error_msg = f"Download error: {critical_error}"
            update_progress(file_progress=0, error=error_msg, download_id=download_id)
            
            # We'll raise exception only for serious errors
            if not os.path.exists(output_filename):
                raise Exception(error_msg)
    
    # If file exists but there were warnings, still mark as success
    if os.path.exists(output_filename):
        # Ensure we show 100% for this file
        update_progress(file_progress=1.0, download_id=download_id)
        return output_filename
    else:
        raise Exception(f"Download failed: {stderr}")

def embed_metadata(mp3_file, title, artist):
    update_progress(task=f"Embedding metadata for: {os.path.basename(mp3_file)}")
    try:
        audio = EasyID3(mp3_file)
    except Exception:
        audio = EasyID3()
    audio["title"] = title
    audio["artist"] = artist
    audio.save(mp3_file)

@app.route("/")
def index():
    return send_file(HTML_FILE)

@app.route("/check_progress")
def check_progress():
    """API endpoint to poll for progress updates"""
    download_id = request.args.get('download_id', 'default')
    
    try:
        progress_file = os.path.join(DOWNLOAD_DIR, f"progress_{download_id}.json")
        with open(progress_file, 'r') as f:
            progress_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        progress_data = reset_progress(download_id)
    
    return jsonify(progress_data)

@app.route("/login")
def login():
    sp_oauth = SpotifyOAuth(
        client_id=os.environ.get("SPOTIPY_CLIENT_ID"),
        client_secret=os.environ.get("SPOTIPY_CLIENT_SECRET"),
        redirect_uri=os.environ.get("SPOTIPY_REDIRECT_URI", "http://[YOUR_IP]:7667/callback"),
        scope="user-library-read playlist-modify-public playlist-modify-private"
    )
    auth_url = sp_oauth.get_authorize_url()
    return redirect(auth_url)

@app.route("/callback")
def callback():
    sp_oauth = SpotifyOAuth(
        client_id=os.environ.get("SPOTIPY_CLIENT_ID"),
        client_secret=os.environ.get("SPOTIPY_CLIENT_SECRET"),
        redirect_uri=os.environ.get("SPOTIPY_REDIRECT_URI", "http://[YOUR_IP]:7667/callback"),
        scope="user-library-read playlist-modify-public playlist-modify-private"
    )
    code = request.args.get('code')
    if code:
        token_info = sp_oauth.get_access_token(code)
        if token_info:
            return "Spotify token generated successfully. You may now try your download again."
        else:
            return "Failed to get token.", 400
    else:
        return "Authorization failed. No code provided.", 400

@app.route("/process", methods=["POST"])
def process():
    """Process the Spotify URL and start background download task"""
    spotify_url = request.form.get("spotify_url")
    if not spotify_url:
        return jsonify({"error": "Spotify URL is required."}), 400
    
    # Generate a unique ID for this download
    download_id = str(uuid.uuid4())
    
    # Initialize progress tracking for this download
    reset_progress(download_id)
    update_progress(status="starting", task="Initializing...", download_id=download_id)
    
    # Start download in background thread
    ACTIVE_DOWNLOADS[download_id] = {
        "status": "starting",
        "url": spotify_url,
        "start_time": time.time()
    }
    
    thread = threading.Thread(
        target=background_download,
        args=(spotify_url, download_id),
        daemon=True
    )
    thread.start()
    
    # Return immediately with the download ID
    return jsonify({
        "status": "processing",
        "download_id": download_id,
        "message": "Download started in background."
    })

@app.route("/download_file", methods=["GET"])
def download_file():
    """Serve the downloaded file from the cache directory."""
    filename = request.args.get("filename")
    if not filename:
        return "Filename is required.", 400
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(file_path):
        return "File not found.", 404
    return send_file(file_path, as_attachment=True)

@app.route("/download_status", methods=["GET"])
def download_status():
    """Check if a background download has completed"""
    download_id = request.args.get('download_id')
    print(f"Checking download status for ID: {download_id}")
    
    if not download_id or download_id not in ACTIVE_DOWNLOADS:
        print(f"Download ID {download_id} not found in {list(ACTIVE_DOWNLOADS.keys())}")
        return jsonify({"status": "not_found"}), 404
    
    download_info = ACTIVE_DOWNLOADS[download_id]
    print(f"Download info for {download_id}: {download_info}")
    
    response_data = None
    
    if "error" in download_info:
        response_data = {
            "status": "error",
            "error": download_info["error"]
        }
    elif "result" in download_info:
        response_data = {
            "status": "complete",
            **download_info["result"]
        }
    else:
        response_data = {"status": "processing"}
    
    print(f"Sending response for {download_id}: {response_data}")
    return jsonify(response_data)

# Function to run downloads in background
def background_download(spotify_url, download_id):
    try:
        result = process_download(spotify_url, download_id)
        print(f"Download complete for {download_id}. Result: {result}")
        # Make sure we store the complete result data
        ACTIVE_DOWNLOADS[download_id] = {
            'status': "complete",
            'result': result,
            'url': spotify_url,
            'complete_time': time.time()
        }
    except Exception as e:
        error_msg = f"Download error: {str(e)}"
        print(f"Background download error: {error_msg}")
        print(traceback.format_exc())
        update_progress(status="error", error=error_msg, force_percent=100, download_id=download_id)
        ACTIVE_DOWNLOADS[download_id] = {
            'status': "error",
            'error': error_msg,
            'url': spotify_url,
            'error_time': time.time()
        }

# Move main download logic to this function
def process_download(spotify_url, download_id="default"):
    update_progress(status="connecting", task="Connecting to Spotify API...", download_id=download_id)
    try:
        sp = create_spotify_client()
    except Exception as e:
        update_progress(status="error", error=f"Spotify client error: {str(e)}", download_id=download_id)
        raise e

    # For single track
    if "track" in spotify_url:
        update_progress(status="fetching", task="Fetching track metadata...", download_id=download_id)
        try:
            track = sp.track(spotify_url)
            title = track['name']
            artist = track['artists'][0]['name']
            artwork = track.get('album', {}).get('images', [{}])[0].get('url', '')
        except Exception as e:
            update_progress(status="error", error=f"Error fetching track metadata: {str(e)}", download_id=download_id)
            raise e
        
        query = f"{artist} - {title}"
        output_filename = f"{artist} - {title}.mp3"
        output_file = os.path.join(DOWNLOAD_DIR, output_filename)
        
        update_progress(status="downloading", total_files=1, current_file=1, download_id=download_id)
        try:
            download_track(query, output_file, 1, 1, download_id)
            update_progress(task="Embedding metadata...", download_id=download_id)
            embed_metadata(output_file, title, artist)
            update_progress(status="complete", task="Download complete!", force_percent=100, download_id=download_id)
        except Exception as e:
            update_progress(status="error", error=f"Error downloading track: {str(e)}", download_id=download_id)
            raise e
        
        return {
            "status": "success",
            "type": "track",
            "title": title,
            "artist": artist,
            "artwork": artwork,
            "download_url": f"/download_file?filename={output_filename}"
        }
    
    # For playlist or album
    elif "playlist" in spotify_url or "album" in spotify_url:
        update_progress(status="fetching", task="Fetching collection metadata...", download_id=download_id)
        try:
            if "playlist" in spotify_url:
                playlist_data = sp.playlist(spotify_url)
                raw_name = playlist_data.get('name', 'Tracks')
                # Use first image if available
                artwork = playlist_data.get('images', [{}])[0].get('url', '')
                results = sp.playlist_tracks(spotify_url)
            else:
                album_data = sp.album(spotify_url)
                raw_name = album_data.get('name', 'Tracks')
                artwork = album_data.get('images', [{}])[0].get('url', '')
                results = sp.album_tracks(spotify_url)
            zip_name = sanitize_filename(raw_name)
        except Exception as e:
            update_progress(status="error", error=f"Error fetching collection metadata: {str(e)}", download_id=download_id)
            raise e

        zip_filename = f"{zip_name}.zip"
        zip_filepath = os.path.join(DOWNLOAD_DIR, zip_filename)
        tracks = results.get('items', [])
        total_tracks = len(tracks)
        
        update_progress(status="downloading", total_files=total_tracks, 
                        task=f"Preparing to download {total_tracks} tracks...", download_id=download_id)
        
        downloaded_files = []
        failed_tracks = []
        
        for i, item in enumerate(tracks, 1):
            try:
                track_info = item['track'] if "track" in item else item
                title = track_info['name']
                artist = track_info['artists'][0]['name']
                query = f"{artist} - {title}"
                output_filename = f"{artist} - {title}.mp3"
                output_file = os.path.join(DOWNLOAD_DIR, output_filename)
                
                try:
                    download_track(query, output_file, i, total_tracks, download_id)
                    embed_metadata(output_file, title, artist)
                    downloaded_files.append(output_file)
                except Exception as e:
                    error_msg = f"Error downloading '{title}': {e}"
                    print(error_msg)
                    update_progress(error=error_msg, failed_track={"title": title, "artist": artist}, download_id=download_id)
                    failed_tracks.append(title)
                    # Continue with next track despite error
            except Exception as e:
                error_msg = f"Error processing track {i}: {e}"
                print(error_msg)
                update_progress(error=error_msg, download_id=download_id)
                # Continue with next track despite error
        
        if not downloaded_files:
            update_progress(status="error", error="No tracks were downloaded.", force_percent=100, download_id=download_id)
            raise Exception("No tracks were downloaded.")

        update_progress(status="packaging", task="Creating ZIP archive...", force_percent=95, download_id=download_id)
        try:
            with zipfile.ZipFile(zip_filepath, "w") as zipf:
                for file in downloaded_files:
                    zipf.write(file, arcname=os.path.basename(file))
        except Exception as e:
            update_progress(status="error", error=f"Error creating ZIP file: {str(e)}", download_id=download_id)
            raise e

        # Create success message
        success_message = "Download complete!"
        if failed_tracks:
            success_message += f" ({len(failed_tracks)} of {total_tracks} tracks failed)"
        
        update_progress(status="complete", task=success_message, force_percent=100, download_id=download_id)
        
        return {
            "status": "success",
            "type": "batch",
            "name": raw_name,
            "artwork": artwork,
            "download_url": f"/download_file?filename={zip_filename}"
        }
    
    else:
        update_progress(status="error", error="Unsupported URL type.", download_id=download_id)
        raise ValueError("Unsupported URL type.")

if __name__ == "__main__":
    # Ensure progress file is reset at startup
    reset_progress()
    app.run(host="0.0.0.0", port=7667, debug=True)