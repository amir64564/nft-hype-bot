FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m bot && mkdir -p /data && chown bot /data
USER bot
ENV DATABASE_URL=sqlite:////data/hypebot.db
VOLUME /data
CMD ["python", "-m", "hypebot"]
