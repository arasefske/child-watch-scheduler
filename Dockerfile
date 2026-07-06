FROM python:3.11-slim

WORKDIR /app

# Install dependencies as a separate layer so Docker cache skips this
# step on rebuilds when only app code changed (not requirements).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY scheduler.py .

EXPOSE 8501

CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
