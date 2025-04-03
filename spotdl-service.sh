# This file can be placed within /usr/local/etc/rc.d/ directory to run on bootup
# You can manually start/stop this service with the commands below:
# sudo /usr/local/etc/rc.d/spotdl-service.sh [start/stop]
# Real-time logs can be seen with the ffollowing bash command:
# tail -f /var/log/youtubedl.log
#!/bin/sh
# Configuration
USER="user"  # Your host username
APP_DIR="/volume/spotdl" # Application directory
VENV_DIR="$APP_DIR/venv"
SCRIPT_NAME="spotdl.sh"
PID_FILE="/var/run/spotdl.pid"
LOG_FILE="/var/log/spotdl.log"
PORT="7667"  # The port your app uses

case "$1" in
    start)
        echo "Starting Spotify Downloader service..."

        # Create log file if it doesn't exist
        touch "$LOG_FILE"
        chown "$USER" "$LOG_FILE"

        # Start the application as the specified user
        cd "$APP_DIR"
        su - "$USER" -c "cd $APP_DIR && ./spotdl.sh >> $LOG_FILE 2>&1 &"

        # Need to get PID from the process since we're using a script
        sleep 5
        PID=$(ps -ef | grep "flask run --host=0.0.0.0 --port=7667" | grep -v grep | awk '{print $2}')
        if [ -n "$PID" ]; then
            echo $PID > "$PID_FILE"
            echo "Spotify Downloader service started with PID: $PID"
        else
            echo "Failed to start Spotify Downloader service or find PID"
            exit 1
        fi
        ;;

    stop)
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            echo "Stopping Spotify Downloader service (PID: $PID)..."
            kill -TERM $PID 2>/dev/null

            # Also kill any associated Python processes
            PIDS=$(ps -ef | grep "flask run --host=0.0.0.0 --port=7667" | grep -v grep | awk '{print $2}')
            for P in $PIDS; do
                kill -TERM $P 2>/dev/null
            done

            rm -f "$PID_FILE"
            echo "Spotify Downloader service stopped."
        else
            echo "Spotify Downloader service is not running."
        fi
        ;;

    restart)
        $0 stop
        sleep 2
        $0 start
        ;;

    status)
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            if ps -p $PID > /dev/null; then
                echo "Spotify Downloader service is running (PID: $PID)"
                exit 0
            else
                echo "PID file exists but process is not running."
                rm -f "$PID_FILE"
                exit 1
            fi
        else
            echo "Spotify Downloader service is not running."
            exit 1
        fi
        ;;

    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
exit 0
