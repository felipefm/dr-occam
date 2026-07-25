import sqlite3
import os

# Caminho para o arquivo do banco de dados criado pelo Docker.
# database.py usa DATABASE_URL="sqlite:///./data/occam.db" por padrão,
# então o arquivo fica em app/data/occam.db a partir da raiz do projeto.
DB_PATH = "app/data/occam.db"

# (name, url, source_type, active, max_daily_articles)
# source_type precisa ser a string crua do Enum ("RSS"), não "SourceType.RSS".
SOURCES = [
    ("G1 Mundo", "https://g1.globo.com/rss/g1/mundo/", "RSS", 1, 3),
    ("Tecnoblog", "https://tecnoblog.net/feed/", "RSS", 1, 3),
]

def insert_sources():
    if not os.path.exists(DB_PATH):
        print(f"Erro: Banco de dados não encontrado em {DB_PATH}.")
        print("Certifique-se de que o FastAPI rodou ao menos uma vez para criar o banco.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        for name, url, source_type, active, max_daily_articles in SOURCES:
            # A tabela source não tem constraint UNIQUE em url/name, então sem
            # esse check rodar o script de novo duplicaria as fontes.
            cursor.execute("SELECT 1 FROM source WHERE url = ?", (url,))
            if cursor.fetchone():
                print(f"Fonte '{name}' já existe no banco (url já cadastrada) — pulando.")
                continue

            cursor.execute("""
                INSERT INTO source (name, url, source_type, active, max_daily_articles)
                VALUES (?, ?, ?, ?, ?)
            """, (name, url, source_type, active, max_daily_articles))
            conn.commit()
            print(f"Fonte '{name}' adicionada com sucesso ao banco SQLite!")
    except Exception as e:
        print(f"Ocorreu um erro ao inserir fontes: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    insert_sources()
