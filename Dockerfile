FROM centos:centos8

RUN yum clean all
RUN yum update -y
RUN yum upgrade -y
RUN yum groupinstall "Development Tools" -y
RUN yum install glibc.i686 libstdc++.i686 wget openssl-devel libffi-devel bzip2-devel -y

# Install Python

WORKDIR /root
RUN wget https://www.python.org/ftp/python/3.9.2/Python-3.9.2.tgz
RUN tar xf Python-3.9.2.tgz
WORKDIR /root/Python-3.9.2
RUN ./configure --enable-optimizations
RUN make altinstall

# Install Valheim base server

RUN useradd valheim
USER valheim

WORKDIR /home/valheim/steamcmd
RUN wget https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz
RUN tar xf steamcmd_linux.tar.gz
RUN ./steamcmd.sh +login anonymous +force_install_dir /home/valheim/server +app_update 896660 validate +quit

# Install Valheim Plus

WORKDIR /home/valheim/server
RUN wget https://github.com/valheimPlus/ValheimPlus/releases/download/0.9.7/UnixServer.tar.gz
RUN tar xf UnixServer.tar.gz


# Install Python server script
RUN pip3.9 install poetry
COPY pyproject.toml ./
RUN python3.9 -m poetry install
COPY server.py ./
COPY start_server_bepinex.sh ./
COPY update.sh ./

ENTRYPOINT [ "python3.9", "-m", "poetry", "run", "python", "server.py" ]

# Web server port
EXPOSE 8080/tcp

# Valheim ports
EXPOSE 2456-2457/tcp
EXPOSE 27015-27030/tcp
EXPOSE 27036-27037/tcp
EXPOSE 2456-2457/udp
EXPOSE 4380/udp
EXPOSE 27000-27031/udp
EXPOSE 27036/udp
