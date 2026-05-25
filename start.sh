#!/usr/bin/env bash
set -e

# Generate Nginx config from template using Render's $PORT
envsubst '$PORT' < /app/nginx.conf.template > /etc/nginx/nginx.conf

# Start Nginx in background
nginx

# Start the main application
python -u main.py
