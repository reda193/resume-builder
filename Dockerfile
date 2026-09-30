FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
      texlive-latex-base texlive-latex-recommended texlive-latex-extra \
      texlive-fonts-recommended lmodern poppler-utils imagemagick \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work