#!/bin/bash

clear

# Set the path to the file uploads directory and datebase directory.
UPLOADS_PATH="/mnt/storage/arkeon/uploads"
DIR_PATH="/home/haruki/arkeon/data"

# Set the port for the application to listen on.
PORT=2926

# Verify Root Permissions.
if [ "$EUID" -ne 0 ]
then
    echo "Error: Run as root."
    exit 1
fi

if [ ! -d "$DIR_PATH" ]
then
    echo "The database directory cannot be found."
    echo "Create a directory and assign permissions."
    mkdir -p "$DIR_PATH"
    chown -R haruki:haruki /home/haruki/personal_drive/data
    chmod 755 /home/haruki/personal_drive/data
fi

# Check if Python packages are installed.
echo "Start package installation..."
python3 -m pip install -r modules.txt
echo "Package installation complete."

# Check if the database needs to be migrated.
echo "Start database migration..."

# If a .env file exists, extract the DATABASE_URL value from it.
if [ -f ".env" ]
then
    ENV_DB_URL=$(grep -E "^DATABASE_URL=" .env | cut -d'=' -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
fi

# If a value is not specified in .env or the file does not exist, use the default value.
if [ -z "$ENV_DB_URL" ]
then
    ENV_DB_URL="sqlite:///./data/account_info.db"
fi

# If the `migrations` folder or `alembic.ini` file does not exist, initialize it once
if [ ! -d "migrations" ] || [ ! -f "alembic.ini" ]
then
    echo "Initializing Alembic..."
    python3 -m alembic init migrations
    
    # It automatically injects the DB URL from the .env file into alembic.ini.
    echo "Configuring alembic.ini with .env URL: $ENV_DB_URL"
    sed -i "s|sqlalchemy.url = .*|sqlalchemy.url = $ENV_DB_URL|g" alembic.ini
fi

# If the migration version folder is empty, create the initial version file.
if [ ! -d "migrations/versions" ] || [ ! "$(ls -A migrations/versions/)" ]
then
    echo "Creating migration files..."
    python3 -m alembic revision --autogenerate -m "init"
fi

# Migration Applied
echo "Applying migrations..."
python3 -m alembic upgrade head
echo "Database migration complete."

# Check if the uploads directory exists, if not create it.
if [ ! -d "$UPLOADS_PATH" ]
then
    echo "Creating uploads directory..."
    mkdir -p "$UPLOADS_PATH"
    echo "Uploads directory created at $UPLOADS_PATH."
else
    echo "Uploads directory already exists at $UPLOADS_PATH."
fi

# Start the application using uvicorn and cloudflared.
echo "Starting the application..."
echo -e "\e[31mRun the \`setup2.sh\` file in a new terminal to complete the web server setup.\e[0m"
python3 -m uvicorn main:app --host 127.0.0.1 --port $PORT --reload
