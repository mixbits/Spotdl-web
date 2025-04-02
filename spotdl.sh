#!/bin/bash
# Change to the project directory
cd /volume/spotdl

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
fi

# Set Flask environment variables
export FLASK_APP=spotdl.py
export FLASK_ENV=production

# Start the Flask app on port 7667
echo "Starting Flask app on port 7667..."
flask run --host=0.0.0.0 --port=7667