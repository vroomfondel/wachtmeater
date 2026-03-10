ARG python_version=3.14
ARG debian_version=slim-trixie

FROM python:${python_version}-${debian_version}

# repeat without defaults in this build-stage
ARG python_version
ARG debian_version

RUN apt update && \
    apt -y full-upgrade && \
    apt -y install htop procps iputils-ping locales vim tini bind9-dnsutils \
                   make cmake gcc g++ libolm-dev && \
    pip install --upgrade pip && \
    rm -rf /var/lib/apt/lists/*

RUN sed -i -e 's/# de_DE.UTF-8 UTF-8/de_DE.UTF-8 UTF-8/' /etc/locale.gen && \
    locale-gen && \
    update-locale LC_ALL=de_DE.UTF-8 LANG=de_DE.UTF-8 && \
    rm -f /etc/localtime && \
    ln -s /usr/share/zoneinfo/Europe/Berlin /etc/localtime

ARG TARGETOS
ARG TARGETARCH
RUN echo "I'm building for $TARGETOS/$TARGETARCH"

ARG UID=1200
ARG GID=1201
ARG UNAME=pythonuser
RUN groupadd -g ${GID} -o ${UNAME} && \
    useradd -m -u ${UID} -g ${GID} -o -s /bin/bash ${UNAME}

ENV PATH="/home/${UNAME}/.local/bin:$PATH"

WORKDIR /app

COPY --chown=${UID}:${GID} runcli.sh ./

COPY --chown=${UID}:${GID} requirements.txt ./
COPY --chown=${UID}:${GID} README.md pyproject.toml wachtmeater.toml.example ./
COPY --chown=${UID}:${GID} wachtmeater ./wachtmeater

RUN runuser -u ${UNAME} -- env PATH="/home/${UNAME}/.local/bin:$PATH" \
        pip3 install --no-cache-dir --upgrade -r ./requirements.txt && \
    runuser -u ${UNAME} -- env PATH="/home/${UNAME}/.local/bin:$PATH" \
        pip install --no-cache-dir -e . && \
    rm -rf /var/lib/apt/lists/*

USER ${UNAME}

# set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ARG gh_ref=gh_ref_is_undefined
ENV GITHUB_REF=$gh_ref
ARG gh_sha=gh_sha_is_undefined
ENV GITHUB_SHA=$gh_sha
ARG buildtime=buildtime_is_undefined
ENV BUILDTIME=$buildtime

ENTRYPOINT ["tini", "--"]
CMD ["wachtmeater", "--help"]
