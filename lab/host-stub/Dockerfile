FROM python:3.12-alpine
RUN apk add --no-cache docker-cli docker-cli-compose
COPY host.py /app/host.py
CMD ["python", "-u", "/app/host.py"]
