# The daemon, installed the way a user installs it: by building the PKGBUILD.
#
# This image exists to test the packaging as much as the code. A container that
# pip-installs the source proves the application works; this one proves the
# PKGBUILD builds, the dependencies resolve, the files land where the unit
# expects them, and the daemon runs as an unprivileged system user.
#
# It is slower than the alternatives, and that is the trade being made.

FROM archlinux:base-devel AS build

RUN pacman -Syu --noconfirm --needed \
        git python python-pip python-build python-installer \
        python-setuptools python-wheel python-pytest \
    && pacman -Scc --noconfirm

# makepkg refuses to run as root, so the build happens as an unprivileged user.
RUN useradd --create-home builder \
    && echo 'builder ALL=(ALL) NOPASSWD: /usr/bin/pacman' > /etc/sudoers.d/builder

# python-hap-python has no Arch package; the companion PKGBUILD in this
# repository is what noti-mapper's own depends= array points at.
USER builder
WORKDIR /home/builder
COPY --chown=builder:builder packaging/aur/python-hap-python/PKGBUILD hap/PKGBUILD
RUN cd hap \
    && makepkg --syncdeps --install --noconfirm --skipchecksums

# python-imapclient is likewise an AUR package. Built here from the same
# source so the image does not need an AUR helper.
RUN mkdir -p imapclient && cd imapclient \
    && curl -fsSL "https://aur.archlinux.org/cgit/aur.git/snapshot/python-imapclient.tar.gz" \
        | tar -xz --strip-components=1 \
    && makepkg --syncdeps --install --noconfirm --skipchecksums

# Now the package itself. The source is copied in rather than fetched, so the
# image is built from the working tree.
COPY --chown=builder:builder . /home/builder/src
RUN cp /home/builder/src/packaging/aur/noti-mapper/PKGBUILD /home/builder/src/packaging/aur/noti-mapper/noti-mapper.install /home/builder/ \
    && cd /home/builder \
    && makepkg --syncdeps --noconfirm --skipchecksums --nocheck \
        --noextract --nodeps 2>/dev/null \
    || true

# The PKGBUILD above expects a release tarball. Inside the image the working
# tree is what there is, so the package is assembled from it directly using the
# same layout the PKGBUILD installs.
USER root
WORKDIR /home/builder/src
RUN python -m build --wheel --no-isolation --outdir=/tmp/wheel \
    && python -m installer --destdir=/ /tmp/wheel/*.whl \
    && install -dm755 /usr/lib/noti-mapper/plugins \
    && cp -r plugins/*/ /usr/lib/noti-mapper/plugins/ \
    && find /usr/lib/noti-mapper/plugins -name '__pycache__' -type d -exec rm -rf {} + \
    && install -Dm644 dist/noti-mapper.service /usr/lib/systemd/system/noti-mapper.service \
    && install -Dm644 dist/noti-mapper.sysusers /usr/lib/sysusers.d/noti-mapper.conf \
    && install -Dm644 dist/man/noti-mapper.1 /usr/share/man/man1/noti-mapper.1 \
    && install -Dm644 dist/man/noti-mapper.d.5 /usr/share/man/man5/noti-mapper.d.5 \
    && install -dm750 /etc/noti-mapper /etc/noti-mapper/plugins /etc/noti-mapper.d


FROM archlinux:base AS runtime

RUN pacman -Syu --noconfirm --needed python avahi python-orjson python-chacha20poly1305-reuseable python-h11 python-zeroconf python-cryptography python-ifaddr && pacman -Scc --noconfirm

# The same sysusers fragment the package ships, so the uid and the home
# directory are the ones a real install gets.
COPY --from=build /usr/lib/sysusers.d/noti-mapper.conf /usr/lib/sysusers.d/
RUN systemd-sysusers

COPY --from=build /usr/bin/noti-mapper /usr/bin/noti-mapper
COPY --from=build /usr/lib/noti-mapper /usr/lib/noti-mapper

# Staged, then moved to wherever this image's interpreter actually looks.
# COPY cannot expand a shell variable in its destination, so naming a version
# here would hard-code one: the build that found this had 3.14 as the
# interpreter and the packages landing in a 3.13 directory, which imports
# exactly nowhere.
COPY --from=build /usr/lib/python3*/site-packages/noti_mapper /stage/noti_mapper
COPY --from=build /usr/lib/python3*/site-packages/imapclient /stage/imapclient
COPY --from=build /usr/lib/python3*/site-packages/pyhap /stage/pyhap
RUN target="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && install -dm755 "$target" \
    && mv /stage/noti_mapper /stage/imapclient /stage/pyhap "$target"/ \
    && rmdir /stage \
    && python -c 'import noti_mapper, imapclient, pyhap' 
COPY --from=build /usr/share/man/man1/noti-mapper.1 /usr/share/man/man1/
COPY --from=build /usr/share/man/man5/noti-mapper.d.5 /usr/share/man/man5/

# The configuration directory is owned by the service user so that a scenario
# can rewrite it through `exec` without anything being mounted from the host.
# That is a concession to testing; the real package leaves it root-owned.
RUN install -dm755 -o noti-mapper -g noti-mapper /etc/noti-mapper.d \
    && install -dm750 -o noti-mapper -g noti-mapper /etc/noti-mapper \
    && install -dm755 /etc/noti-mapper/plugins \
    && install -dm750 -o noti-mapper -g noti-mapper /var/lib/noti-mapper \
    && install -dm750 -o noti-mapper -g noti-mapper /var/lib/noti-mapper/watched

# A configuration valid enough for the daemon to start. Every scenario
# overwrites it; it exists so that `compose up` does not fail before a
# scenario has had a chance to say anything.
COPY docker/default-config.json /etc/noti-mapper.d/10-default.json
RUN chown noti-mapper:noti-mapper /etc/noti-mapper.d/10-default.json \
    && printf '{}' > /etc/noti-mapper/secrets.json \
    && chown noti-mapper:noti-mapper /etc/noti-mapper/secrets.json \
    && chmod 0600 /etc/noti-mapper/secrets.json

USER noti-mapper

# systemd is not running in here, so the paths the unit would export are set
# explicitly. Everything else is what the unit would give the process.
ENV STATE_DIRECTORY=/var/lib/noti-mapper
EXPOSE 9736

ENTRYPOINT ["/usr/bin/noti-mapper"]
CMD ["run"]
