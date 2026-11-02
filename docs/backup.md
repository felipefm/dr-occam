# Backup semanal do banco

**Status:** em produção desde 2026-11-02. Código em `app/backup/`, agendamento em `app/scheduler.py`.

## O que faz

Uma vez por semana (padrão: **domingo às 03:30**, horário de Brasília), o Dr. Occam copia o banco `app/data/occam.db` para:

```
app/data/backups/occam-AAAA-MM-DD-HHMMSS.db
```

A pasta guarda as **4 cópias mais novas**; a cada backup, as mais antigas são apagadas. A ideia é que um script do próprio usuário leve as cópias para outro disco ou para a nuvem: o que for movido para fora da pasta não é tocado, e a retenção só conta o que ainda está nela.

No `/admin`, a seção **"💾 Backup semanal"** mostra o próximo backup, o último feito, o erro do último que falhou (se houver), as cópias na pasta, e tem o botão **"Fazer backup agora"**.

## Como a cópia é feita

1. Confere se há espaço em disco (tamanho do banco + 20%).
2. Copia com a API de backup do SQLite para um arquivo `.partial`. É a mesma cópia usada antes de cada deploy: com o banco em modo WAL, ela lê um retrato consistente sem travar a varredura nem ser corrompida por ela.
3. Deixa a cópia em modo de journal normal (um arquivo só, sem `-wal`/`-shm`) e roda `PRAGMA quick_check`.
4. Só então renomeia para `.db`. **Um `.db` na pasta é sempre uma cópia completa e conferida.** Um `.partial` é sobra de uma cópia interrompida e é apagado no backup seguinte.
5. Ajusta o dono do arquivo para o mesmo da pasta `app/data` (o container roda como root).

O backup agendado **espera a varredura terminar** se ela estiver rodando (até 3 h; depois disso copia mesmo assim, o que é seguro). O botão do `/admin` copia na hora. Nunca rodam dois backups ao mesmo tempo.

Se o container estiver fora do ar no horário, aquele backup se perde (o agendador não guarda histórico entre reinícios). Para cobrir isso, ao subir o container, se o último backup bem-sucedido tiver mais de 7 dias, ou se nunca houve backup, é feito um backup de recuperação 10 minutos depois. Por isso, **o primeiro backup acontece 10 minutos depois do deploy**.

O resultado (último sucesso e último erro) fica em `app/data/backup-status.json`, fora da pasta de backups, para continuar valendo depois que as cópias forem movidas. Também vai para o log.

## Tempo e tamanho

Medido em 2026-11-02, com o banco em 61 MB: de 2 a 30 s. O tempo varia com a memória livre da máquina. O banco cresce ~450 notícias por dia, ~1,3 GB por ano.

## Configuração (`.env`)

| Variável | Padrão | |
|---|---|---|
| `BACKUP_ENABLED` | `true` | `false` desliga o agendamento; o botão continua funcionando. |
| `BACKUP_WEEKDAY` | `sun` | `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`. |
| `BACKUP_TIME` | `03:30` | Horário de Brasília. |
| `BACKUP_KEEP` | `4` | Cópias mantidas na pasta. |
| `BACKUP_DIR` | `app/data/backups` | No container, `/app/data/backups`. |
| `BACKUP_MAX_WAIT_MINUTES` | `180` | Espera máxima pela varredura. |

Mudar uma variável exige recriar o container (`docker compose up -d`).

## Como restaurar

1. Pare o container: `docker stop dr_occam_api`.
2. Guarde o banco atual, por segurança: `mv app/data/occam.db app/data/occam.db.antes-restauracao` (e apague `occam.db-wal` e `occam.db-shm`, se existirem: eles pertencem ao banco antigo).
3. Copie a cópia escolhida: `cp app/data/backups/occam-AAAA-MM-DD-HHMMSS.db app/data/occam.db`.
4. Suba o container: `docker start dr_occam_api`.

Para só consultar uma cópia sem restaurar, abra-a com qualquer cliente SQLite (a busca por vetores precisa da extensão `sqlite-vec`).

## O que não faz

- **Não leva as cópias para fora deste disco.** Isso fica a cargo do script do usuário; enquanto as cópias estiverem só em `app/data/backups`, um problema no disco leva o banco e os backups juntos.
- Não apaga os `.bak` antigos, feitos à mão antes dos deploys, em `app/data`.
