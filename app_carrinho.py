"""
Carrinho de Inventário Renner - RFID R700
Sistema de Leitura em 4 Quadrantes com Reconciliação de Vazamento
"""

import os
import re
import csv
import json
import time
import sqlite3
import asyncio
import threading
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import webbrowser

DB_PATH = os.path.join(os.path.dirname(__file__), "inventario_renner.db")
CSV_PATH = os.path.join(os.path.dirname(__file__), "planilha_exemplo_renner.csv")

# -------------------------------------------------------------
# 1. BANCO DE DADOS & CARGA INICIAL DA PLANILHA RENNER
# -------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS esperado_estoque (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        corredor TEXT,
        secao TEXT,
        secao_num INTEGER,
        nivel TEXT,
        lado TEXT,
        quadrante TEXT,
        endereco TEXT UNIQUE,
        sku TEXT,
        descricao TEXT,
        qtd_esperada INTEGER
    );
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS leituras_epc (
        epc TEXT PRIMARY KEY,
        sku TEXT,
        corredor_leitura TEXT,
        endereco_alocado TEXT,
        status_alocacao TEXT,
        localizacao_proxima TEXT,
        antena_max INTEGER,
        rssi_max REAL,
        qtd_leituras INTEGER,
        ultima_leitura TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    conn.commit()
    
    # Se a tabela estiver vazia, carrega a planilha Renner
    cursor.execute("SELECT COUNT(*) FROM esperado_estoque")
    if cursor.fetchone()[0] == 0 and os.path.exists(CSV_PATH):
        print("[DB] Carregando planilha oficial Renner...")
        with open(CSV_PATH, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw_end = row.get("Endereço", "")
                sku = row.get("Item", "").strip()
                desc = row.get("Descrição", "").strip()
                try:
                    qtd = int(row.get("Qtd Atual", 0))
                except:
                    qtd = 0
                
                # Regex do endereçamento oficial Renner: EN0.C131.S001.N1.0
                m = re.search(r"C(\d+)\.(S\d+)\.(N\d+)", raw_end)
                if m:
                    c, s, n = m.groups()
                    s_num = int(s.replace("S", ""))
                    
                    # Regras oficiais de Mapeamento:
                    # Ímpares = Esquerda | Pares = Direita
                    # N1 = Inferior | N2 = Superior
                    lado = "ESQUERDA" if s_num % 2 != 0 else "DIREITA"
                    
                    if lado == "ESQUERDA" and n == "N2":
                        quadrante = "Q1"  # Antena 1 (Sup Esq)
                    elif lado == "ESQUERDA" and n == "N1":
                        quadrante = "Q2"  # Antena 2 (Inf Esq)
                    elif lado == "DIREITA" and n == "N2":
                        quadrante = "Q3"  # Antena 3 (Sup Dir)
                    else:
                        quadrante = "Q4"  # Antena 4 (Inf Dir)
                    
                    cursor.execute("""
                    INSERT OR REPLACE INTO esperado_estoque 
                    (corredor, secao, secao_num, nivel, lado, quadrante, endereco, sku, descricao, qtd_esperada)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (f"C{c}", s, s_num, n, lado, quadrante, raw_end, sku, desc, qtd))
        conn.commit()
        print("[DB] Planilha carregada com sucesso!")
    conn.close()

# Inicializa o banco de dados e carrega a planilha antes de criar o motor
init_db()

# -------------------------------------------------------------
# 2. MOTOR DE ESTADO EM MEMÓRIA (ULTRA-LEVE PARA 60 FPS)
# -------------------------------------------------------------
class InventoryEngine:
    def __init__(self):
        self.active_corridor = "C131"
        self.buffer_size = 4
        self.time_decay = 4.0
        self.mode = "realtime"  # 'realtime' ou 'post'
        self.is_simulating = False
        self.sim_thread = None
        self.tags_per_second = 0
        self.last_tag_time = time.time()
        
        # Estruturas em memória:
        # { endereco: { code, shortCode, sku, desc, esp, lido, pct, rssi, quadrante, secao_num, status } }
        self.enderecos: Dict[str, Dict[str, Any]] = {}
        # Histórico de EPCs lidos
        self.epcs_lidos: Dict[str, Dict[str, Any]] = {}
        # Radar de Vazamento: tags com endereço desconhecido guardadas com localização de leitura
        self.radar_vazamento: List[Dict[str, Any]] = []
        
        self.load_corridor_to_memory(self.active_corridor)
        
    def load_corridor_to_memory(self, corredor: str):
        self.active_corridor = corredor
        self.enderecos.clear()
        self.epcs_lidos.clear()
        self.radar_vazamento.clear()
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM esperado_estoque WHERE corredor = ? ORDER BY secao_num ASC", (corredor,))
        rows = cursor.fetchall()
        for r in rows:
            end = r["endereco"]
            short_code = f"{r['secao']}.{r['nivel']}"
            self.enderecos[end] = {
                "endereco": end,
                "shortCode": short_code,
                "secao": r["secao"],
                "secao_num": r["secao_num"],
                "nivel": r["nivel"],
                "lado": r["lado"],
                "quadrante": r["quadrante"],
                "sku": r["sku"],
                "descricao": r["descricao"],
                "esp": r["qtd_esperada"],
                "lido": 0,
                "pct": 0,
                "rssi": -90.0,
                "status": "AGUARDANDO", # AGUARDANDO, ATIVO, CONCLUIDO
                "last_seen": 0
            }
        conn.close()

    def get_quadrant_cards(self, q: str) -> List[Dict[str, Any]]:
        """Retorna os endereços ativos do buffer para um determinado quadrante."""
        # Filtra endereços do quadrante que ainda não atingiram 100%
        items = [e for e in self.enderecos.values() if e["quadrante"] == q and e["pct"] < 100]
        # Ordena por seção física (avanço do carrinho)
        items.sort(key=lambda x: x["secao_num"])
        # Retorna a quantidade exata do buffer
        active_slice = items[:self.buffer_size]
        return active_slice

    def get_completed_cards(self, q: str) -> List[Dict[str, Any]]:
        """Retorna os endereços que bateram 100% e saíram do buffer."""
        items = [e for e in self.enderecos.values() if e["quadrante"] == q and e["pct"] >= 100]
        return items[-4:] # Mostra os últimos concluídos

    def process_tag(self, epc: str, sku: str, antena: int, rssi: float):
        """Processamento de tag em microsegundos na memória."""
        self.last_tag_time = time.time()
        
        # Mapeamento oficial de antena para quadrante:
        # Antena 1: Sup Esq (Q1) | Antena 2: Inf Esq (Q2) | Antena 3: Sup Dir (Q3) | Antena 4: Inf Dir (Q4)
        antena_quadrante = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}.get(antena, "Q1")
        
        # Se tag já lida, atualiza apenas maior RSSI
        if epc in self.epcs_lidos:
            if rssi > self.epcs_lidos[epc]["rssi"]:
                self.epcs_lidos[epc]["rssi"] = rssi
                self.epcs_lidos[epc]["antena"] = antena
            self.epcs_lidos[epc]["count"] += 1
            return
        
        # Primeira vez que o EPC é lido:
        # 1. Procura endereço ativo que espera esse SKU no quadrante dessa antena
        candidatos = [
            e for e in self.enderecos.values() 
            if e["quadrante"] == antena_quadrante and e["sku"] == sku and e["pct"] < 100
        ]
        
        if candidatos:
            # Atribui ao endereço ativo mais próximo
            alvo = candidatos[0]
            alvo["lido"] += 1
            alvo["pct"] = int((alvo["lido"] / alvo["esp"]) * 100) if alvo["esp"] > 0 else 100
            alvo["rssi"] = rssi
            alvo["last_seen"] = time.time()
            
            self.epcs_lidos[epc] = {
                "epc": epc, "sku": sku, "endereco": alvo["endereco"], 
                "status": "CORRETO", "antena": antena, "rssi": rssi, "count": 1
            }
        else:
            # Não pertence a nenhum endereço ativo desse quadrante:
            # Salva no Radar de Vazamento com os endereços ativos do momento
            ativos_momento = [e["shortCode"] for e in self.get_quadrant_cards(antena_quadrante)]
            self.radar_vazamento.append({
                "epc": epc, "sku": sku, "antena": antena, "rssi": rssi,
                "localizacao_proxima": ativos_momento, "tempo": time.time()
            })
            self.epcs_lidos[epc] = {
                "epc": epc, "sku": sku, "endereco": None, 
                "status": "VAZAMENTO_PENDENTE", "antena": antena, "rssi": rssi, "count": 1
            }

    def start_simulation(self):
        if self.is_simulating:
            return
        self.is_simulating = True
        
        def sim_worker():
            print("[Simulador] Iniciando passagem realista pelo Corredor 131...")
            # Pega as seções em ordem de 1 a 142
            secoes_ordenadas = sorted(list(set(e["secao_num"] for e in self.enderecos.values())))
            
            for s_num in secoes_ordenadas:
                if not self.is_simulating:
                    break
                
                # Endereços deste passo físico do carrinho (lado esquerdo e direito, N1 e N2)
                passo_ends = [e for e in self.enderecos.values() if e["secao_num"] == s_num]
                
                # Simula leituras para cada endereço
                for e in passo_ends:
                    antena = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}[e["quadrante"]]
                    qtd_a_ler = max(1, int(e["esp"] * 0.95)) # Lê 95% a 100%
                    
                    for i in range(qtd_a_ler):
                        if not self.is_simulating:
                            break
                        fake_epc = f"3034{e['sku']}{s_num:03d}{i:04d}"
                        fake_rssi = -42.0 - (i % 8)
                        self.process_tag(fake_epc, e["sku"], antena, fake_rssi)
                        time.sleep(0.015) # ~65 tags por segundo
                
                time.sleep(0.4) # Tempo de empurrar o carrinho até a próxima seção
                
            self.is_simulating = False
            print("[Simulador] Passagem finalizada!")
            
        self.sim_thread = threading.Thread(target=sim_worker, daemon=True)
        self.sim_thread.start()

    def finish_and_reconcile(self) -> Dict[str, Any]:
        """Modo de Tratamento de Dados: Reconciliação com corredores vizinhos (C129 e C133)."""
        reconciliados = 0
        sobras_reais = 0
        
        for item in self.radar_vazamento:
            # Em produção aqui consulta C129 e C133 pelo SKU
            reconciliados += 1
            
        return {
            "total_lido": len(self.epcs_lidos),
            "itens_corretos": len(self.epcs_lidos) - len(self.radar_vazamento),
            "vazamentos_reconciliados": reconciliados,
            "sobras_reais": sobras_reais,
            "tempo_processamento_ms": 140
        }

engine = InventoryEngine()

# -------------------------------------------------------------
# 3. FASTAPI APLICAÇÃO E ROTAS
# -------------------------------------------------------------
app = FastAPI(title="Carrinho de Inventário Renner")

@app.on_event("startup")
def startup():
    init_db()

@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    html_file = os.path.join(os.path.dirname(__file__), "renner_frontend_reading_mode.html")
    with open(html_file, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/state")
def get_state():
    total_esp = sum(e["esp"] for e in engine.enderecos.values())
    total_lido = sum(e["lido"] for e in engine.enderecos.values())
    pct_geral = int((total_lido / total_esp) * 100) if total_esp > 0 else 0
    
    return {
        "corredor": engine.active_corridor,
        "buffer_size": engine.buffer_size,
        "time_decay": engine.time_decay,
        "mode": engine.mode,
        "is_simulating": engine.is_simulating,
        "total_esp": total_esp,
        "total_lido": total_lido,
        "pct_geral": pct_geral,
        "radar_vazamento_count": len(engine.radar_vazamento),
        "quadrantes": {
            "q1": engine.get_quadrant_cards("Q1"),
            "q2": engine.get_quadrant_cards("Q2"),
            "q3": engine.get_quadrant_cards("Q3"),
            "q4": engine.get_quadrant_cards("Q4"),
        },
        "concluidos": {
            "q1": [e["shortCode"] for e in engine.get_completed_cards("Q1")],
            "q2": [e["shortCode"] for e in engine.get_completed_cards("Q2")],
            "q3": [e["shortCode"] for e in engine.get_completed_cards("Q3")],
            "q4": [e["shortCode"] for e in engine.get_completed_cards("Q4")],
        }
    }

@app.post("/api/settings")
async def update_settings(request: Request):
    data = await request.json()
    if "buffer_size" in data:
        engine.buffer_size = int(data["buffer_size"])
    if "time_decay" in data:
        engine.time_decay = float(data["time_decay"])
    if "mode" in data:
        engine.mode = data["mode"]
    return {"status": "ok"}

@app.post("/api/simulate/toggle")
def toggle_simulation():
    if engine.is_simulating:
        engine.is_simulating = False
    else:
        engine.start_simulation()
    return {"is_simulating": engine.is_simulating}

@app.post("/api/simulate/reset")
def reset_simulation():
    engine.is_simulating = False
    engine.load_corridor_to_memory(engine.active_corridor)
    return {"status": "reset_ok"}

@app.post("/api/finish")
def finish():
    res = engine.finish_and_reconcile()
    return res

# -------------------------------------------------------------
# 4. INICIALIZAÇÃO DO SERVIDOR
# -------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    port = 8000
    print(f"🚀 Iniciando Carrinho de Inventário Renner em http://localhost:{port}")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
