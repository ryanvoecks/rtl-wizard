FROM python:3.12-alpine

COPY policy_proxy.py /app/policy_proxy.py

# read_only filesystem in compose makes __pycache__ writes fatal.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER nobody
WORKDIR /app

EXPOSE 2375
ENTRYPOINT ["python", "policy_proxy.py"]
