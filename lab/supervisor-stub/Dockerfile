FROM python:3.12-alpine
COPY server.py /app/server.py
EXPOSE 80 8123
HEALTHCHECK --interval=3s --timeout=2s --retries=15 CMD wget -q -O /dev/null http://127.0.0.1:8123/ || exit 1
CMD ["python", "-u", "/app/server.py"]
