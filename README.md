# Contador de Veículos O/D

Aplicação para Windows que lê o vídeo de uma câmera de cruzamento (live do YouTube ou arquivo) e gera dados de tráfego:
CSVs de veículos (origem, destino, tempo parado) e de fila por braço, mais gráficos PNG. Projeto de faculdade.

> **Estado:** repositório privado em preparação. Ainda **sem licença** definida (decisão do grupo pendente).

## Como rodar

1. **Baixe os modelos** na aba *Releases* deste repositório e coloque os `.onnx` na pasta `03-Modelos/` (veja abaixo).
2. Dê dois cliques em **`Iniciar_ContadorOD.bat`**.
   - Na primeira vez ele cria o ambiente (`.venv`) e instala as bibliotecas: 5 a 15 minutos, cerca de 250 MB.
   - Depois disso abre direto.
3. Siga a aba **1. Fonte do vídeo**, depois ROIs, Saída e Execução. Mais detalhes em [`LEIA-ME.txt`](LEIA-ME.txt).

Requisitos: Windows 10 ou 11, **Python 3.12, 3.13 ou 3.14** (testado no 3.13) e internet na primeira execução.
Placa de vídeo é opcional: com GPU (NVIDIA, AMD ou Intel) a aplicação usa o DirectML; sem GPU, roda na CPU.

## Modelos

Os modelos **não ficam no código** (são arquivos binários de ~10 MB). Estão nos *Releases*:

| Arquivo | Para quê |
| --- | --- |
| `vehicle_stage2_full_v5_best.onnx` | **Dia** (padrão) |
| `vehicle_stage4_noite_luz_best.onnx` | **Noite**: treinado também com imagens noturnas |

Coloque os dois em `03-Modelos/`. Na aba **Saída > Avançado > Modelo** escolha Dia ou Noite. O arquivo `SHA256SUMS.txt` do
Release permite conferir o download.

O modelo da noite é um **piloto** (80 imagens noturnas no treino): detalhes, números e limites em
[`docs_modelo/MODELO_NOITE_LEIA-ME.md`](docs_modelo/MODELO_NOITE_LEIA-ME.md).

## Estrutura

| Pasta | Conteúdo |
| --- | --- |
| `01-Config/` | ROIs e imagem de referência de cada câmera já configurada |
| `02-Codigo/` | código da aplicação (`painel/` = janela, `nucleo/` = leitura, detecção, passagens, CSVs, gráficos) |
| `03-Modelos/` | aqui entram os `.onnx` baixados dos Releases (não versionados) |
| `04-Saida/` | onde os dados são gravados (não versionado) |
| `docs_modelo/` | documentação do modelo da noite |

## Observações importantes

- As lives usadas nas configurações (YouTube) podem sair do ar ou ter termos de uso próprios. Confira antes de redistribuir
  imagens ou dados delas.
- Os modelos foram treinados com Ultralytics/YOLO (licença AGPL-3.0). Defina a licença do projeto levando isso em conta.
- As imagens usadas no treino do modelo da noite vêm do Pexels e **não** estão neste repositório.
