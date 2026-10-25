#!/usr/bin/env python3
"""
Tenta criar uma instância ARM A1.Flex (Always Free) na Oracle Cloud e avisa por push (ntfy.sh)
quando conseguir.

Dois modos:
  --ci     Uma tentativa por execução, lendo tudo de variáveis de ambiente (GitHub Actions).
  (padrão) Loop local a cada 10 min, lendo ~/.oci/config.
"""
import argparse
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import oci

# ---------------- Configuração ----------------
INTERVALO_SEGUNDOS = 600          # 10 minutos (só no modo local)
OCPUS = 2                         # 2 OCPUs (o limite grátis total é 4)
MEMORIA_GB = 12                   # 12 GB (o limite grátis total é 24)
DISCO_GB = 100                    # grátis até 200 GB no total
NOME_INSTANCIA = "servidor-gratis"
SO = "Canonical Ubuntu"
SO_VERSAO = "24.04"
SHAPE = "VM.Standard.A1.Flex"

PASTA = Path.home() / ".oracle-retry"
PASTA.mkdir(exist_ok=True)
ARQ_TOPICO = PASTA / "ntfy_topic.txt"
ARQ_LOG = PASTA / "tentativas.log"
ARQ_OK = PASTA / "SUCESSO.txt"


def log(msg):
    linha = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(linha, flush=True)
    try:
        with open(ARQ_LOG, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass


# ---------------- Configuração da conta ----------------
def carregar_config():
    if os.environ.get("OCI_KEY"):
        cfg = {
            "user": os.environ["OCI_USER"].strip(),
            "fingerprint": os.environ["OCI_FINGERPRINT"].strip(),
            "tenancy": os.environ["OCI_TENANCY"].strip(),
            "region": os.environ["OCI_REGION"].strip(),
            "key_content": os.environ["OCI_KEY"],
        }
    else:
        cfg = oci.config.from_file()
    oci.config.validate_config(cfg)
    return cfg


# ---------------- Notificação (ntfy) ----------------
def topico():
    if os.environ.get("NTFY_TOPIC"):
        return os.environ["NTFY_TOPIC"].strip()
    if not ARQ_TOPICO.exists():
        ARQ_TOPICO.write_text(f"oracle-retry-{random.randint(10**9, 10**10 - 1)}")
    return ARQ_TOPICO.read_text().strip()


def avisar(titulo, texto, prioridade="default"):
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topico()}",
            data=texto.encode("utf-8"),
            headers={"Title": titulo.encode("ascii", "ignore").decode("ascii"),
                     "Priority": prioridade},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log(f"Falha ao enviar push: {e}")


def sinalizar_fim():
    """No GitHub Actions, avisa o workflow que ele pode se desligar."""
    saida = os.environ.get("GITHUB_OUTPUT")
    if saida:
        with open(saida, "a", encoding="utf-8") as f:
            f.write("fim=true\n")


# ---------------- Descoberta de recursos ----------------
def chave_ssh_publica():
    if os.environ.get("SSH_PUBLIC_KEY"):
        return os.environ["SSH_PUBLIC_KEY"].strip()
    for nome in ("id_ed25519.pub", "id_rsa.pub"):
        p = Path.home() / ".ssh" / nome
        if p.exists():
            return p.read_text().strip()
    destino = Path.home() / ".ssh" / "id_ed25519"
    destino.parent.mkdir(exist_ok=True)
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(destino)], check=True)
    return destino.with_suffix(".pub").read_text().strip()


def achar_subnet(rede, compartimento):
    """Usa uma subnet pública existente; se não houver nenhuma, cria a rede básica."""
    for s in rede.list_subnets(compartimento).data:
        if not s.prohibit_public_ip_on_vnic:
            return s.id
    log("Nenhuma subnet pública encontrada. Criando rede básica (VCN + subnet pública)...")
    m = oci.core.models
    vcn = rede.create_vcn(m.CreateVcnDetails(
        compartment_id=compartimento, cidr_block="10.0.0.0/16", display_name="vcn-gratis")).data
    ig = rede.create_internet_gateway(m.CreateInternetGatewayDetails(
        compartment_id=compartimento, vcn_id=vcn.id, is_enabled=True, display_name="ig-gratis")).data
    rede.update_route_table(vcn.default_route_table_id, m.UpdateRouteTableDetails(
        route_rules=[m.RouteRule(destination="0.0.0.0/0", destination_type="CIDR_BLOCK",
                                 network_entity_id=ig.id)]))
    sub = rede.create_subnet(m.CreateSubnetDetails(
        compartment_id=compartimento, vcn_id=vcn.id, cidr_block="10.0.0.0/24",
        display_name="subnet-publica", prohibit_public_ip_on_vnic=False,
        route_table_id=vcn.default_route_table_id)).data
    return sub.id


def achar_imagem(computo, compartimento):
    imgs = computo.list_images(
        compartimento, operating_system=SO, operating_system_version=SO_VERSAO, shape=SHAPE,
        sort_by="TIMECREATED", sort_order="DESC").data
    if not imgs:
        raise RuntimeError(f"Nenhuma imagem {SO} {SO_VERSAO} para {SHAPE}")
    return imgs[0].id


# ---------------- Tentativa ----------------
def tentar_uma_vez(cfg):
    ident = oci.identity.IdentityClient(cfg)
    computo = oci.core.ComputeClient(cfg)
    rede = oci.core.VirtualNetworkClient(cfg)
    comp = cfg["tenancy"]

    # Se já existe uma instância com esse nome ativa, não cria outra.
    for i in computo.list_instances(comp, display_name=NOME_INSTANCIA).data:
        if i.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return ("ja_existe", i.id)

    subnet = achar_subnet(rede, comp)
    imagem = achar_imagem(computo, comp)
    ssh = chave_ssh_publica()
    dominios = ident.list_availability_domains(comp).data
    random.shuffle(dominios)

    for ad in dominios:
        try:
            det = oci.core.models.LaunchInstanceDetails(
                compartment_id=comp,
                availability_domain=ad.name,
                display_name=NOME_INSTANCIA,
                shape=SHAPE,
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=OCPUS, memory_in_gbs=MEMORIA_GB),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=imagem, boot_volume_size_in_gbs=DISCO_GB),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnet, assign_public_ip=True),
                metadata={"ssh_authorized_keys": ssh},
            )
            inst = computo.launch_instance(det).data
            return ("criada", inst.id)
        except oci.exceptions.ServiceError as e:
            texto = f"{e.status} {e.code} {e.message}"
            if "capacity" in texto.lower() or e.status in (429, 500, 502, 503):
                log(f"Sem capacidade em um dos domínios: {e.status} {e.code}")
                continue
            return ("erro_fatal", texto)
    return ("sem_capacidade", None)


def ip_da_instancia(cfg, instancia_id):
    computo = oci.core.ComputeClient(cfg)
    rede = oci.core.VirtualNetworkClient(cfg)
    try:
        oci.wait_until(computo, computo.get_instance(instancia_id), "lifecycle_state",
                       "RUNNING", max_wait_seconds=600)
        att = computo.list_vnic_attachments(cfg["tenancy"], instance_id=instancia_id).data[0]
        return rede.get_vnic(att.vnic_id).data.public_ip
    except Exception as e:
        log(f"Não consegui ler o IP ainda: {type(e).__name__}")
        return "(veja no console da Oracle)"


def uma_rodada(cfg, ci):
    """Faz uma tentativa. Devolve True quando não precisa mais tentar."""
    try:
        status, info = tentar_uma_vez(cfg)
    except oci.exceptions.ServiceError as e:
        status, info = "erro_fatal", f"{e.status} {e.code} {e.message}"
    except Exception as e:
        status, info = "erro_rede", f"{type(e).__name__}: {e}"

    if status == "criada":
        ip = ip_da_instancia(cfg, info)
        # No GitHub Actions o log é público: o IP vai só no push, nunca no log.
        log("Instância criada! O IP foi enviado por push." if ci else f"Instância criada! IP: {ip}")
        if not ci:
            ARQ_OK.write_text(f"IP: {ip}\nID: {info}\n")
        avisar("Servidor Oracle criado!", f"IP: {ip} | usuario: ubuntu", "urgent")
        sinalizar_fim()
        return True
    if status == "ja_existe":
        log(f"Já existe uma instância '{NOME_INSTANCIA}'. Parando.")
        sinalizar_fim()
        return True
    if status == "erro_fatal":
        log(f"Erro que não adianta repetir: {info}")
        avisar("Script do Oracle parou", str(info)[:300], "high")
        sinalizar_fim()
        sys.exit(1)
    log(f"Resultado: {status}.")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ci", action="store_true", help="uma tentativa só, config por variáveis de ambiente")
    ap.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()

    if args.test_notify:
        avisar("Teste", "Se voce viu isso, o aviso funciona.")
        log("Push de teste enviado.")
        return

    cfg = carregar_config()

    if args.ci:
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            avisar("Teste do aviso", "Tudo certo: o robo comecou a tentar criar o servidor.")
        uma_rodada(cfg, ci=True)
        return

    log("Iniciado em modo local (loop a cada 10 min).")
    while not uma_rodada(cfg, ci=False):
        time.sleep(INTERVALO_SEGUNDOS)


if __name__ == "__main__":
    main()
