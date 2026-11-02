"""Coleta sem janela, a partir do JSON de ROIs salvo pelo painel (útil para testes e servidores).

Rodar de dentro da pasta PROJETO_V2:
  .venv\\Scripts\\python 02-Codigo\\coletar.py --rois 01-Config\\4_corners_camera_downtown.json --minutos 5
  .venv\\Scripts\\python 02-Codigo\\coletar.py --rois 01-Config\\x.json --fonte C:\\videos\\gravacao.mp4
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nucleo.leitor import inicio_pelo_nome  # noqa: E402
from nucleo.motor import ConfigColeta, Motor  # noqa: E402
from painel.caminhos import modelo_padrao, pasta_saida_padrao  # noqa: E402
from painel.fonte import resolver_fonte  # noqa: E402

MODELO_PADRAO = modelo_padrao()


def main():
    ap = argparse.ArgumentParser(description="Coleta O/D sem janela")
    ap.add_argument("--rois", required=True, help="JSON salvo na aba 2 do painel")
    ap.add_argument("--fonte", help="link ou arquivo (padrão: a fonte gravada no JSON)")
    ap.add_argument("--saida", default=str(pasta_saida_padrao()))
    ap.add_argument("--intervalo", type=int, default=15, help="minutos por arquivo: 5, 10, 15, 30 ou 60")
    ap.add_argument("--minutos", type=float, help="duração; sem isso roda até Ctrl+C (ou o fim do vídeo)")
    ap.add_argument("--ate", help="roda até este horário (HH:MM)")
    ap.add_argument("--modelo", default=str(MODELO_PADRAO))
    ap.add_argument("--imgsz", type=int, default=960)
    ap.add_argument("--fps", type=float, default=6)
    ap.add_argument("--anotado", action="store_true")
    ap.add_argument("--gpu", action="store_true", help="processar na GPU (DirectML); cai para a CPU se falhar")
    ap.add_argument("--conf", type=float, default=0.25, help="confiança mínima da detecção (0 a 1)")
    a = ap.parse_args()

    rois = json.loads(Path(a.rois).read_text(encoding="utf-8"))
    entrada = a.fonte or rois["fonte"]
    info = resolver_fonte(entrada)
    modo, horario = "parar", None
    if a.minutos:
        modo = "minutos"
    if a.ate:
        modo = "horario"
        h, m = map(int, a.ate.split(":"))
        horario = datetime.now().astimezone().replace(hour=h, minute=m, second=0, microsecond=0)
    inicio_video = None
    if info.tipo == "arquivo":
        inicio_video = inicio_pelo_nome(info.entrada, datetime.now().astimezone().tzinfo)
    snapshot = Path(a.rois).parent / rois.get("snapshot", "")
    cfg = ConfigColeta(
        snapshot=str(snapshot) if rois.get("snapshot") and snapshot.exists() else None,
        camera_id=rois["camera_id"], video_id=info.video_id, fonte=entrada, stream_url=info.stream_url,
        ao_vivo=info.ao_vivo, rois_json=rois, pasta_saida=a.saida, modelo=a.modelo,
        intervalo_min=a.intervalo, modo_duracao=modo, minutos=a.minutos or 0, horario=horario,
        video_anotado=a.anotado, imgsz=a.imgsz, fps_alvo=a.fps, inicio_video=inicio_video,
        conf_minima=a.conf, dispositivo="gpu" if a.gpu else "cpu",
        reabrir=(lambda: resolver_fonte(entrada).stream_url) if info.ao_vivo else None,
    )

    def mostrar(e):
        p = f" {e.progresso * 100:4.0f}%" if e.progresso is not None else ""
        print(f"[{e.estado:12s}]{p} {e.decorrido_s:6.0f}s  {e.quadros_s:4.1f} q/s  na tela {e.veiculos_na_tela:2d}  "
              f"parados {e.parados_agora:2d}  registros {e.registros:4d}  reconexões {e.reconexoes}"
              + (f"  ⚠ {e.aviso}" if e.aviso else ""), flush=True)

    ultimo = [0.0]

    def ao_estado(e):
        if time.monotonic() - ultimo[0] >= 10 or e.estado == "encerrado":
            ultimo[0] = time.monotonic()
            mostrar(e)

    motor = Motor(cfg, ao_estado=ao_estado)
    print(f"{info.titulo} | {'ao vivo' if info.ao_vivo else 'vídeo'} | ROIs: {len(rois['rois'])} | saída: {a.saida}")
    ultimo_proc = [""]

    def ao_estado_proc(e):
        if e.provedor and e.provedor != ultimo_proc[0]:
            ultimo_proc[0] = e.provedor
            print(f"processando em: {e.provedor}" + (f"  ⚠ {e.aviso_fixo}" if e.aviso_fixo else ""))
        ao_estado(e)
    motor.ao_estado = ao_estado_proc
    motor.iniciar()
    try:
        while motor.rodando():
            motor.aguardar(0.5)
    except KeyboardInterrupt:
        print("parando: fechando os arquivos...")
        motor.parar()
        motor.aguardar()
    e = motor.estado
    print(f"fim: {e.motivo_fim} | registros {e.registros} {dict(e.situacoes)} | passagens {dict(e.passagens)}")
    if e.erro:
        print("ERRO:", e.erro)


if __name__ == "__main__":
    main()
