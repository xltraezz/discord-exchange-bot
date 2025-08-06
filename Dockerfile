# Use the official Python 3.11 slim image
FROM python:3.11-slim

# 1) Set a working directory
WORKDIR /app

# 2) Copy & install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) Copy your bot code
COPY . .

# 4) Run your bot
CMD ["python", "bot.py"]