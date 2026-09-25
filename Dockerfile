FROM registry.fedoraproject.org/fedora-minimal:42@sha256:09a2061e2cfb85ac8e7fa7f2234d0ace6ad4f2b7dfdf0f257c90405e4f07577d
RUN microdnf install -y --setopt=install_weak_deps=0 skopeo python3 && microdnf clean all
COPY otk /opt/otk/otk
ENV PYTHONPATH=/opt/otk PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN printf '#!/bin/sh\nexec python3 -m otk "$@"\n' > /usr/local/bin/otk && chmod 755 /usr/local/bin/otk \
 && useradd -u 1000 -m otk
USER 1000
WORKDIR /work
ENTRYPOINT ["otk"]
