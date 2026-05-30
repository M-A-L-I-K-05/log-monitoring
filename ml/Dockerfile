# Python 3.11 — под него у prophet/numpy/pyod есть готовые manylinux-колёса
# (на 3.12 prophet иногда требует сборки stan).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Prophet 1.1.6 кладёт «обрезанный» bundled cmdstan (только скомпилированная
# модель), а cmdstanpy>=1.3 в validate_cmdstan_path требует наличие makefile.
# Компиляция не нужна (модель уже собрана) — достаточно создать пустой makefile,
# чтобы пройти валидацию пути. См. разбор в ходе реализации ML-сервиса.
RUN find /usr/local/lib/python3.11/site-packages/prophet/stan_model \
        -maxdepth 1 -type d -name "cmdstan-*" -exec touch {}/makefile \;

COPY . .

EXPOSE 8006

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8006"]
