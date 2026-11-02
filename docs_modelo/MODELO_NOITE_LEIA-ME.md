# Modelo de detecção de veículos com ajuste para cenas noturnas (`vehicle_stage4_noite_luz`)

Este é o `vehicle_stage2_full_v5` (o modelo que já usamos) treinado mais um pouco, agora com **imagens noturnas reais** e
**variações de luz**. A ideia é achar mais veículos à noite, sem estragar o dia.

**Resumo em três linhas**
- À noite, no teste separado de 29 imagens, o mAP50 subiu de **0,64** para **0,79** (recall de 0,62 para 0,69).
- De dia, no teste original do dataset, o mAP50 caiu de **0,70** para **0,68**: uma queda pequena, perto do ruído.
- Ele recebe a **imagem normal**, sem nenhum tratamento. Nenhuma mudança no código da aplicação é necessária: é só trocar o arquivo `.onnx`.

Os números são de um piloto com poucos dados. Leia a seção **Limites** antes de citar qualquer um deles.

---

## 1. O que está nesta pasta

| Arquivo | O que é |
|---|---|
| `vehicle_stage4_noite_luz_best.onnx` | **O modelo para usar na aplicação.** Entrada 1x3x960x960 fixa, uma classe (`vehicle`), nomes de classe nos metadados. |
| `vehicle_stage4_noite_luz_best.pt` | Os mesmos pesos no formato Ultralytics, para retreinar ou exportar de novo. |
| `treino_e_avaliacao/results.csv` | Curva do treino, época por época. |
| `treino_e_avaliacao/args.yaml` | Todos os parâmetros do treino. |
| `treino_e_avaliacao/avaliacao_noite.txt` | Saída bruta da avaliação noturna (teste + câmera de Tóquio). |
| `treino_e_avaliacao/avaliacao_dia.txt` | Saída bruta da avaliação diurna. |
| `treino_e_avaliacao/night_split.csv` | Lista das 130 imagens noturnas e em qual divisão (treino, validação ou teste) cada uma ficou. |
| `scripts/` | Os scripts usados (ver seção 7). |

As imagens do dataset **não** estão aqui (vêm do Pexels).

## 2. Como usar

**Na aplicação de janela (PySide6):** aba **Saída > Avançado > Modelo de detecção (.onnx)**, e escolher o arquivo
`vehicle_stage4_noite_luz_best.onnx`. Em `PROJETO_V2/03-Modelos/` ele pode ficar ao lado do modelo antigo.

- O modelo antigo (`vehicle_stage2_full_v5_best.onnx`) continua sendo o melhor para **dia**. Recomendamos ter os dois e
  escolher conforme o horário da câmera.
- Resolução de entrada: 960 (o mesmo do modelo antigo).
- Os dois modelos funcionam com a imagem original. Não é necessário aplicar CLAHE nem outro tratamento.

## 3. O que foi feito

1. **Diagnóstico.** À noite, o modelo antigo perdia carros escuros, sem farol aceso, e ônibus, vans e caminhões. O dataset
   tinha pouquíssimas imagens escuras *com* veículos: das 375 imagens de treino, só 62 eram escuras (brilho médio abaixo
   de 70) e várias eram cenas sem carro. A validação do dataset tinha só 10 imagens escuras, então ela nem conseguia medir
   a noite.
2. **Testes que NÃO resolveram** (feitos antes, em 251 quadros noturnos de uma câmera de Tóquio, comparando com um
   YOLOv8m como referência):
   - Tratar a imagem na inferência com **gamma**, **retinex simples**, **remoção de ruído** ou **ganho automático**: sem ganho
     (gamma e retinex pioraram).
   - **Preto e branco**: piorou o recall (de 78% para 73%). Foi descartado.
   - **CLAHE** (realce de contraste local) na inferência: ajudou (recall de 78% para 86% no corte 0,4), mas exige tratar
     todo quadro.
   - Retreinar só com **aumentos de luz**, sem imagens noturnas novas: não ajudou. O modelo não aprendeu a dispensar o CLAHE.
3. **Imagens noturnas reais.** Baixamos 320 imagens noturnas de trânsito do Pexels (16 buscas: cruzamentos, rodovias,
   chuva, ônibus, caminhões, táxis, Tóquio etc.).
4. **Rótulos automáticos, corrigidos à mão.** Um script gerou caixas automáticas (YOLOv8m + modelo antigo, depois de
   aplicar CLAHE) e o grupo **corrigiu no labelme**: apagou caixas erradas, descartou imagens sem veículo útil.
   - Regra combinada: **no máximo 10 caixas por imagem**. Imagens com mais ficaram de fora (se ficassem só 10 caixas numa
     imagem com 20 veículos, os outros 10 virariam "fundo" no treino).
   - Resultado: **130 imagens corrigidas, 537 caixas**.
5. **Dataset novo** (`dataset/yolo_night`, o dataset original ficou intacto): dia + noite.
   - Treino: 455 imagens (375 originais + 80 noturnas).
   - Validação: 101 imagens (80 originais + 21 noturnas), usada para escolher a época.
   - **Teste noturno: 29 imagens** que nunca entram no treino nem na escolha de época.
   - A divisão das imagens noturnas foi **por grupo de busca** do Pexels, e não por imagem, para fotos parecidas
     (mesmo fotógrafo, mesma rua) não caírem dos dois lados.
6. **Treino.** Fine-tuning a partir do `vehicle_stage2_full_v5`, com o dia e a noite misturados (para não esquecer o dia)
   e **variações de luz sorteadas a cada vez que uma imagem é vista**:
   - CLAHE (probabilidade 0,5, intensidade 2 a 4),
   - brilho e contraste (0,5),
   - gamma (0,3),
   - ruído (0,3).
   Sem preto e branco. Hiperparâmetros: AdamW, lr0 1e-4, `warmup_bias_lr` 0, imagem 960, `close_mosaic` 0, no máximo
   60 épocas, parada antecipada com paciência 15. Rodou 31 épocas e a melhor foi a **16**. O tamanho de lote escolhido
   automaticamente foi 2 (GPU de 4 GB).
7. **Duas variantes para comparar**, com o mesmo treino:
   - **controle**: dados noturnos, sem variações de luz;
   - **CLAHE sempre**: CLAHE em todas as imagens de treino (a aplicação precisaria aplicar CLAHE em todo quadro).
   Escolhemos a versão "luz aleatória" porque ganha à noite **sem** exigir mudança na aplicação.

## 4. Resultados

### Noite: teste separado, 29 imagens fora do treino (ônibus e chuva)

| Modelo | Entrada | Precisão | Recall | mAP50 | mAP50-95 |
|---|---|---|---|---|---|
| `v5` (antigo) | original | 0,742 | 0,618 | 0,636 | 0,401 |
| noite controle | original | 0,930 | 0,561 | 0,709 | 0,453 |
| **noite + luz aleatória (este modelo)** | **original** | **0,788** | **0,692** | **0,787** | **0,492** |
| noite + CLAHE sempre | com CLAHE | 0,896 | 0,647 | 0,799 | 0,518 |

### Dia: teste original do dataset, 80 imagens, imagem original

| Modelo | Precisão | Recall | mAP50 | mAP50-95 |
|---|---|---|---|---|
| `v5` (antigo) | 0,751 | 0,689 | 0,701 | 0,304 |
| noite controle | 0,728 | 0,658 | 0,690 | 0,310 |
| **noite + luz aleatória (este modelo)** | **0,741** | **0,664** | **0,683** | **0,293** |
| noite + CLAHE sempre | 0,729 | 0,659 | 0,684 | 0,301 |

### Câmera real de Tóquio, à noite: 251 quadros, referência YOLOv8m (2.673 veículos), corte 0,4

| Modelo | Recall (imagem original) | Caixas fora da referência |
|---|---|---|
| `v5` (antigo) | 81% | 20% |
| noite controle | 92% | 27% |
| **noite + luz aleatória (este modelo)** | **87%** | **27%** |
| noite + CLAHE sempre | 80% | 22% |

"Recall" aqui é a fração dos veículos que o YOLOv8m achou e que o nosso modelo também achou. "Fora da referência" é a
fração das nossas caixas que o YOLOv8m não viu: podem ser carros que ele perdeu ou falsos alarmes (não conferimos a olho).

## 5. Limites (leia antes de citar os números)

- **Poucos dados noturnos.** Só 80 imagens noturnas entram no treino. Os números são de um piloto, e diferenças pequenas
  (da ordem de 0,01 a 0,03) estão no ruído.
- **O teste noturno é estreito:** 29 imagens de dois grupos (ônibus e chuva), todas do Pexels, a mesma fonte do treino.
  Ele **não prova** desempenho em câmera de trânsito real. A comparação com a câmera de Tóquio toca nisso, mas usa um
  modelo de referência que também erra à noite, e não conferimos a olho as caixas extras.
- **Imagens com mais de 10 caixas ficaram de fora do treino.** O modelo viu pouco engarrafamento noturno denso.
- **Fotos do Pexels não são câmera de trânsito:** costumam ter exposição longa, rastros de luz e cores fortes.
- **No dia há uma queda pequena** (mAP50 0,701 para 0,683, recall 0,689 para 0,664). Com 80 imagens está perto do ruído,
  mas é consistente nos três modelos novos. Por isso recomendamos manter o modelo antigo para o dia.
- **Não foi testado no pipeline inteiro** (viagens origem para destino, CSVs). Só a detecção foi avaliada.
- Os valores da câmera de Tóquio dependem de uma referência automática, não de rótulos feitos à mão.

## 6. O que falta para melhorar

1. **Mais imagens noturnas**, especialmente **quadros reais de câmera de trânsito** corrigidos à mão.
2. Um **conjunto de teste noturno rotulado à mão** de câmeras diferentes (é o que mais falta para medir de verdade).
3. Mais épocas para a variante "CLAHE sempre", que ainda melhorava na época 60.
4. Testar o modelo no pipeline completo (viagens) com um trecho noturno e outro diurno.

## 7. Como reproduzir

Os scripts em `scripts/` foram escritos para rodar dentro de `build_car_dataset/` do projeto (eles referenciam as
pastas `dataset/` e `runs/` desse projeto) e usam caminhos locais que precisam ser ajustados em outra máquina
(por exemplo, o caminho do `yolov8m.pt` em `prelabel_night.py` e `avaliar_noite.py`).

| Etapa | Script |
|---|---|
| Baixar imagens noturnas do Pexels (precisa de `PEXELS_API_KEY` no ambiente) | `fetch_night_cars.py` |
| Gerar caixas automáticas para corrigir no labelme | `prelabel_night.py` |
| Limitar a 10 caixas por imagem (move as que passam) | `limitar_caixas.py` |
| Montar o dataset dia + noite com o teste noturno separado | `build_night_dataset.py` |
| Treinar (modos `controle`, `clahe`, `clahe_sempre`) | `train_stage3_clahe.py` |
| Avaliar no teste noturno e na câmera de Tóquio | `avaliar_noite.py` |
| Conferir o dia | `avaliar_dia.py` |

O treino usou um ambiente isolado com o `albumentations` instalado só nele (para não alterar o Python global).

**Licença e fontes:** as imagens vêm do Pexels (uso conforme a licença do Pexels). Os modelos Ultralytics/YOLO têm licença
AGPL-3.0; vale conferir isso antes de distribuir a aplicação. Os rótulos automáticos usaram o YOLOv8m (COCO) como ponto de
partida e foram corrigidos à mão.
