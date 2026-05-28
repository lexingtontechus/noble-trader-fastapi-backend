#!/bin/bash
set -e
cd /home/aloysiusteo/noble-trader-backend
source venv/bin/activate
pip install -q -r requirements.txt
pip install -q python-dotenv alpaca-trade-api
echo "SUCCESS: All dependencies installed"