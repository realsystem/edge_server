FROM python:3.12-alpine
COPY server.py /app/server.py
RUN adduser -D -u 1000 stub
USER stub
EXPOSE 8081
HEALTHCHECK --interval=3s --timeout=2s --retries=15 CMD wget -q -O /dev/null http://127.0.0.1:8081/bottoken/getMe || exit 1
CMD ["python", "/app/server.py"]
