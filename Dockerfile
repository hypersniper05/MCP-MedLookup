FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Clone abbreviations data and seed the database
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    git clone https://github.com/imantsm/medical_abbreviations.git /tmp/med_abbr && \
    apt-get remove -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY . .

# Create data directory and seed database
RUN mkdir -p /app/data && \
    DATABASE_PATH=/app/data/medical.db python scripts/seed_db.py /tmp/med_abbr/CSVs /app/data/medical.db && \
    rm -rf /tmp/med_abbr

EXPOSE 8010

CMD ["python", "server.py"]
