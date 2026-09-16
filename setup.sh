#!/bin/bash
# Setup script for local development

set -e

echo "🔧 Setting up AI Newsletter..."

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate and install dependencies
echo "📦 Installing dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Create directories
mkdir -p data logs

echo "✅ Setup complete!"
echo ""
echo "To run the fetcher:"
echo "  source venv/bin/activate"
echo "  python scripts/fetch-news.py"
echo ""
echo "Or use the wrapper:"
echo "  ./scripts/run-fetch.sh"