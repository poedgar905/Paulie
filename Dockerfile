FROM python:3.12-slim

# Install Node.js (for sell.js — TypeScript CLOB client)
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Node.js deps
COPY package.json .
RUN npm install --production

# Copy all code
COPY . .

CMD ["python", "bot.py"]
