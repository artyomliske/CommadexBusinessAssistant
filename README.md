# Commandex Business Assistant

> Production-oriented 

## 

|---|---|| 
| Privacy by design | | | | | | 
## 
```mermaid
flowchart TD
    A[Channels] --> B[Webhooks]
    B --> C[Events]
    C --> D[PostgreSQL]
    C --> E[ARQ Worker]
    E --> F[Agent Pipeline]
    F --> G[Domain State]
    G --> H[Review Policy]
    H --> I[Outbound Queue]
    I --> A
    G --> J[Operations Panel]
    G --> K[Drive Sheets]
```    G --> K[    G --> J[


## Portfolio demo



## 

```bash
make install
make test
make lint
docker compose up -d --build
```


## 
Portfolio-
## 
|---|---|| 
| `demo` | | `tests` | | `src/repairbot/integrations` | | `src/repairbot/web` | | `src/repairbot/outbound` | | `src/repairbot/channels` | | `src/repairbot/agents` | | `src/repairbot/domain` | 
## 

## 
