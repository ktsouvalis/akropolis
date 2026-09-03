# akropolis

*[English](README.md) | Ελληνικά*

Provisioning και monitoring ενός highly-available 3-node [Authentik](https://goauthentik.io) cluster μέσω SSH.

Το akropolis μετατρέπει τρία καθαρά Ubuntu 24.04 VMs σε ένα production-grade Authentik identity provider cluster — PostgreSQL 16 με Patroni auto-failover πάνω από etcd, HAProxy connection routing ανά node, τερματισμό TLS με nginx, και εικονική IP (VIP) μέσω keepalived VRRP — ακολουθώντας τον οδηγό υλοποίησης που αναπτύχθηκε και δοκιμάστηκε στην πράξη στη Μονάδα Ψηφιακής Διακυβέρνησης του Πανεπιστημίου Πελοποννήσου. Τρέχει από τον σταθμό εργασίας σας, δεν απαιτεί τίποτα προεγκατεστημένο στους nodes, και καταλήγει εκδίδοντας ένα έτοιμο config για το συνοδευτικό εργαλείο monitoring.

Πιστοποιητικά από οποιαδήποτε πηγή: self-signed, οποιαδήποτε ACME CA (Let's Encrypt, HARICA ACME, ...), ή εξωτερικά εκδοθέντα αρχεία (π.χ. η ροή του HARICA portal που χρησιμοποιείται ευρέως στον ελληνικό δημόσιο τομέα).

## Αρχιτεκτονική

```
Client → VIP (keepalived/VRRP) → nginx στον MASTER node → οποιοδήποτε από τα 3 Authentik backends (:9443)
                                                          → Authentik → HAProxy (127.0.0.1:5000) → Patroni leader
                                                          → (cache / sessions / tasks / channels όλα στο PostgreSQL)
```

| Συστατικό | Πώς εκτελείται | Σκοπός |
|---|---|---|
| PostgreSQL 16 + Patroni | bare-metal systemd | HA database με αυτόματο failover |
| etcd v3.5 | Docker, `network_mode: host` | DCS: distributed lock + config store του Patroni |
| HAProxy | Docker, `network_mode: host` | PG router ανά node: το `127.0.0.1:5000` φτάνει πάντα στον τρέχοντα leader |
| nginx | Docker, `network_mode: host` | τερματισμός TLS + load balancing μεταξύ των Authentik backends |
| keepalived | bare-metal systemd | VRRP virtual IP με health-tracked failover |
| Authentik | Docker, `network_mode: host` | ο ίδιος ο identity provider |

Χωρίς Redis: το Authentik ≥ 2025.10 κρατά sessions, cache, tasks και WebSocket state μέσα στο PostgreSQL.

## Κατάσταση

| Φάση | Κατάσταση |
|---|---|
| preflight | ✅ υλοποιημένη (read-only) |
| base / etcd / patroni / haproxy | ✅ υλοποιημένες (δεν έχουν ακόμα δοκιμαστεί σε πραγματικούς hosts) |
| tls — `none` / `self_signed` / `acme` (staging) / `import` | ✅ υλοποιημένη, δοκιμασμένη |
| nginx-keepalived (μαζί με ACME finalization) | ✅ υλοποιημένη (τα config αρχεία επαληθεύτηκαν με πραγματικό `nginx -t` / `keepalived -t`) |
| authentik | ✅ υλοποιημένη (το compose αντικατοπτρίζει αυτούσιο το production αρχείο) |
| restore | ✅ υλοποιημένη (προαιρετική — παραλείπεται αν δεν οριστεί `restore.sql_file`) |
| handoff | ✅ υλοποιημένη, δοκιμασμένη (εκδίδει το πραγματικό ak-monitor schema) |
| `akropolis monitor` | 🚧 stub (θα ενσωματώσει το ak-monitor) |

**Το pipeline provisioning είναι πλήρες** — κάθε φάση από preflight έως handoff είναι υλοποιημένη. Το πρώτο πλήρες run πάνω σε πραγματικά VMs παραμένει το εναπομείναν ορόσημο.

## Εγκατάσταση

```bash
git clone https://github.com/ktsouvalis/akropolis.git
cd akropolis
python3 -m venv .venv            # Debian/Ubuntu/Mint: apt install python3-venv αν λείπει
.venv/bin/pip install -e .
.venv/bin/akropolis --version
```

Απαιτήσεις στον **σταθμό εργασίας**: Python ≥ 3.10, πρόσβαση SSH στους nodes.
Απαιτήσεις στους **nodes**: καθαρό Ubuntu 24.04, χρήστης SSH με δυνατότητα root, σωστό MTU interface (1400 σε VXLAN overlays, 1500 σε flat L2). Όλα τα υπόλοιπα είναι δουλειά του akropolis.

## Γρήγορη εκκίνηση

```bash
# 1. απάντησε στις ερωτήσεις μία φορά· υλοποιούνται σε ένα ελέγξιμο αρχείο
.venv/bin/akropolis init                          # → config.<site>.yml

# 2. διάβασε το αρχείο. σοβαρά. αυτό είναι το βήμα ελέγχου πριν αγγίξεις οτιδήποτε.

# 3. read-only validation των nodes — ασφαλές να τρέξει οπουδήποτε, δεν αλλάζει τίποτα
.venv/bin/akropolis provision config.<site>.yml --only preflight

# 4. το πλήρες pipeline (επαναλήψιμο· κάθε φάση ρωτάει πριν εφαρμόσει)
.venv/bin/akropolis provision config.<site>.yml
```

### Ζωντανή πρόοδος

Κάθε μακροχρόνια λειτουργία ανακοινώνει τον εαυτό της *πριν* εκτελεστεί: μια animated γραμμή κατάστασης σε terminal (`… (ak-node-2) bootstrap: pull + database migrations — 130s / 900s`), μια απλή γραμμή `…` όταν η έξοδος γίνεται pipe. Οι αναμονές (προαγωγή leader, ένταξη replica, σύγκλιση backend, health gates containers, έκδοση ACME) μετρούν elapsed/budget επί τόπου. Χωρίς αυτό, ένα δεκάλεπτο pull εικόνας είναι αδιάκριτο από ένα hang· με αυτό, ένα κόκκινο ✘ προηγείται πάντα από το ακριβές πράγμα που ήταν σε εξέλιξη.

## Εντολές

```
akropolis init [-o FILE]            interactive wizard → γράφει config.<site>.yml
akropolis provision CONFIG          εκτελεί το phase pipeline (επαναλήψιμο)
akropolis provision CONFIG --only PHASE [PHASE...]     εκτελεί μόνο τις συγκεκριμένες phases
akropolis provision CONFIG --replay PHASE [PHASE...]   επανεκτελεί ολοκληρωμένες phases
akropolis monitor CONFIG            (stub) θα τρέχει το monitoring TUI
```

## Το μοντέλο των phases

Κάθε phase εκτελεί **plan → confirm → apply → verify**:

- Το **plan** τυπώνει ακριβώς τι θα κάνει το apply, πριν συμβεί οτιδήποτε.
- Το **confirm** — τα sites με `lab` ρωτούν `y/N`· τα `production` sites απαιτούν να πληκτρολογήσετε το όνομα του site. Οι read-only phases (preflight) παραλείπουν το confirm. Αν απαντήσετε αρνητικά, το pipeline σταματά καθαρά.
- Το **apply** κάνει τη δουλειά, streaming γραμμές ✔/✘/⚠ ανά node.
- Το **verify** είναι ένα health gate. Μια phase που κάνει apply αλλά αποτυγχάνει στο verify μαρκάρεται `failed` και **ο runner σταματά** — δεν χτίζει ποτέ πάνω σε ασταθές θεμέλιο.

Η πρόοδος καταγράφεται σε ένα state file ανά site (βλ. παρακάτω), οπότε ένα νέο run παραλείπει τις ολοκληρωμένες phases και συνεχίζει από εκεί που έμεινε. Το `--replay PHASE` μαρκάρει ακριβώς τις ονομαζόμενες phases ως pending — όλα τα υπόλοιπα κρατούν το done-skip τους — και είναι σχεδιασμένο να είναι no-op ή μια ρητή, ανιχνεύσιμη αλλαγή, ποτέ re-bootstrap. Το preflight είναι state-aware: σε ένα run στη μέση του lifecycle, θύρες, containers και το VIP που ανήκουν σε ήδη ολοκληρωμένες phases είναι αναμενόμενα (ο έλεγχος VIP μάλιστα αντιστρέφεται μόλις ολοκληρωθεί το nginx-keepalived — η απάντηση γίνεται πλέον η υγιής κατάσταση), και υπολειμματικά ευρήματα όπως μια ένδειξη χαμηλού δίσκου ή τα ίχνη μιας phase που γίνεται replay υποβαθμίζονται σε προειδοποιήσεις. Ένας παρθένος host παίρνει την πλήρη αυστηρή μεταχείριση.

## Αρχείο configuration

Ένα αρχείο YAML ανά site είναι η μοναδική πηγή αλήθειας· το `init` είναι απλώς ένας βολικός τρόπος να το παράγετε. Δείτε το [`config.example.yml`](config.example.yml) για την πλήρη σχολιασμένη αναφορά. Τα βασικά:

```yaml
site:
  name: uop-test            # χρησιμοποιείται στο state, στα prompts, στο εκδιδόμενο monitor config
  environment: lab          # lab | production (το production αυστηροποιεί τα confirmations,
                            #                    απορρίπτει tls provider "none")
provision:
  state_file: .state/uop-test.json
  refuse_existing: true     # το preflight αποτυγχάνει σκληρά σε hosts που ήδη φέρουν cluster

ssh:
  user: root                # ή ένας χρήστης με sudo και become: true
  auth: key                 # key | agent | password (το password ζητείται, ποτέ δεν αποθηκεύεται)
  key_file: ~/.ssh/id_ed25519
  # become: true — κλιμάκωση μέσω sudo. Χρησιμοποιείται passwordless sudo όταν είναι διαθέσιμο·
  # διαφορετικά το password του sudo ζητείται μία φορά ανά run (μέσω sudo -S,
  # ποτέ δεν αποθηκεύεται). Για κλειδιά με passphrase: χρησιμοποιήστε auth: agent (ή key — ο
  # agent δοκιμάζεται πρώτος), ώστε το passphrase να μη φτάνει ποτέ στο akropolis.

nodes:                      # ακριβώς 3· ο πρώτος είναι bootstrap_leader εξ ορισμού
  - { name: ak-node-1, ip: 10.99.97.71, bootstrap_leader: true }
  - { name: ak-node-2, ip: 10.99.97.72 }
  - { name: ak-node-3, ip: 10.99.97.73 }

network:
  vip: 10.99.97.70          # πρέπει να μοιράζεται το /24 των nodes (το VRRP απαιτεί γειτνίαση L2)
  interface: ens18
  expected_mtu: 1400        # 1400 για VXLAN overlays, 1500 για flat L2

tls:
  provider: self_signed     # none | self_signed | acme | import
  hostname: auth.example.gr
  # acme:   { directory_url: ..., email: ..., staging: true }
  # import: { fullchain: ./certs/fullchain.pem, privkey: ./certs/privkey.pem }

# network.trusted_proxies: []   # δείτε "Πίσω από εξωτερικό reverse proxy" παρακάτω

postgres:
  extra_pg_hba: []          # γραμμές ειδικές για το site, προστίθενται στο παραγόμενο pg_hba
                            # π.χ. "host postgres postgres 10.23.2.50/32 scram-sha-256"
base:
  apt_upgrade: false        # τα βασικά πακέτα εγκαθίστανται ούτως ή άλλως·
                            # τα πλήρη dist upgrades μένουν στην πολιτική patching του διαχειριστή
```

Το config επικυρώνεται σε ένα πέρασμα κατά τη φόρτωση: placeholder IPs, ασυμφωνίες υποδικτύου VIP/node, key files που λείπουν, και απαιτήσεις ειδικές ανά provider αναφέρονται όλα μαζί πριν συνδεθεί οτιδήποτε πουθενά.

## Phases

### preflight *(read-only)*

Επικυρώνει και τους τρεις nodes χωρίς να αλλάζει τίποτα. Ασφαλές να τρέξει σε οποιονδήποτε host οποιαδήποτε στιγμή — ακόμα και production, όπου θα πρέπει να απορρίψει με "existing cluster artifacts".

Έλεγχοι ανά node: προσβασιμότητα SSH και root/sudo, έκδοση OS (προειδοποίηση αν δεν είναι Ubuntu 24.04), ύπαρξη interface με το αναμενόμενο MTU, ≥ 20 GB ελεύθερα στο `/`, και οι 14 απαιτούμενες θύρες ελεύθερες (80, 443, 2379, 2380, 5432, 5000, 5001, 8008, 9000, 9080, 9081, 9300, 9301, 9443), απουσία υπαρχόντων ιχνών cluster (`/etc/patroni`, containers etcd/haproxy/nginx/authentik σε εκτέλεση, ενεργοποιημένο keepalived) όταν είναι ενεργό το `refuse_existing`, και ping μεταξύ nodes με το DF bit στο αναμενόμενο MTU (πιάνει λάθος ρύθμιση overhead VXLAN πριν γίνει ανεξήγητο κόλλημα replication). Σε επίπεδο cluster: απόκλιση ρολογιού ≤ 5 δευτ., το VIP να μην απαντά (δεν πρέπει να το κατέχει ακόμα κανείς), και DNS για το `tls.hostname` να επιλύεται στο VIP — σκληρή αποτυχία για `acme`, προειδοποίηση διαφορετικά.

### base

Βήμα 1 του οδηγού. Hostname ανά node, ένα block διαχειριζόμενο από akropolis-marker στο `/etc/hosts` (αφαιρέσιμο και επαναλήψιμο), βασικά πακέτα + chrony, Docker CE από το επίσημο repository της Docker, και UFW: default deny εισερχομένων, επιτρέπεται ssh/80/443/9000 και όλη η κίνηση μεταξύ των τριών IPs των nodes, μετά `--force enable` (ο κανόνας ssh προηγείται πάντα του enable).

Ο monitoring host παίρνει το δικό του άνοιγμα UFW: το `monitor.ip` στο config του site (ή μια διαδραστική ερώτηση, με την απάντηση καρφιτσωμένη στο state — Enter για παράλειψη) επιτρέπεται στα `2379,5000,5001,8008,9000,9443/tcp` σε κάθε node. Το ak-monitor δεν είναι ένας από τους nodes, οπότε χωρίς αυτόν τον κανόνα το default-deny αδειάζει σιωπηλά κάθε στήλη dashboard που δεν είναι απλό HTTPS — τα polls απλώς κάνουν timeout.

`apt upgrade` δεν εκτελείται σκόπιμα εκτός αν οριστεί `base.apt_upgrade: true` — το drift πακέτων ανήκει στην πολιτική patching σας, όχι στον provisioner.

Verify: `docker compose` διαθέσιμο, UFW ενεργό, chrony σε εκτέλεση, hostname εφαρμοσμένο — σε κάθε node.

### etcd

Βήμα 2 του οδηγού. Ένα RAFT cluster 3 nodes (quorum 2) ως DCS του Patroni, από το `gcr.io/etcd-development/etcd:v3.5.30` σε Docker με `network_mode: host`.

Το initial-cluster token παράγεται **μία φορά** και καρφιτσώνεται στο state file, ώστε ένα re-run να μην μπορεί ποτέ να ξανα-bootstrap-άρει ένα σχηματισμένο cluster. Τα rendered compose αρχεία στέλνονται με ανίχνευση checksum· μια αλλαγή config σε ένα container που τρέχει προκαλεί `down && up -d` — ποτέ `restart`, που δεν επανεφαρμόζει volume mounts ή network mode.

Verify: `etcdctl endpoint health` OK και στα τρία client URLs και 3 members σε κατάσταση `started`, με ένα παράθυρο σύγκλισης για φρέσκα ξεκινήματα.

### patroni

Βήμα 3 του οδηγού, και η phase όπου η σειρά bootstrap κάνει ή χαλάει το cluster — κωδικοποιημένη ρητά αντί να ελπίζεται:

1. Το PostgreSQL 16 (repo pgdg) και το venv του Patroni (`/opt/patroni`, `patroni[etcd3]` + `psycopg2-binary`) εγκαθίστανται σε **όλους** τους nodes, τα configs και τα systemd units γίνονται render παντού — αλλά **τίποτα δεν ξεκινά**. Η stock υπηρεσία `postgresql` σταματά και απενεργοποιείται: το Patroni κατέχει τον κύκλο ζωής.
2. Το Patroni ξεκινά **μόνο** στον bootstrap leader, και η phase περιμένει το REST API να απαντήσει `200` στο `/primary` — τη στιγμή που *προάγει τον εαυτό του σε leader* — με budget 300 δευτ. Αν δεν προαχθεί ποτέ, η phase σταματά **πριν** ξεκινήσει οποιοδήποτε replica.
3. Οι replicas ξεκινούν έναν-έναν, ο καθένας περιμένοντας να φτάσει σε `role=replica` με `state=running/streaming` πριν ξεκινήσει ο επόμενος.
4. Ο ρόλος και η βάση `authentik` δημιουργούνται στον leader, ιδεμποτικά.

Το rendered `pg_hba` ξεκινά με `local all postgres peer` — το DCS-managed pg_hba του Patroni **αντικαθιστά** ολοσχερώς το Debian default, και χωρίς μια ρητή γραμμή `local`, το `psql` μέσω Unix socket ως postgres (από το οποίο εξαρτώνται το βήμα 4 και το verify) πεθαίνει με ένα FATAL `no pg_hba.conf entry for host ""`. Περιλαμβάνει επιπλέον τις εγγραφές `127.0.0.1/32` για replication και rewind, η απουσία των οποίων έχει προκαλέσει σιωπηλές αποτυχίες streaming στην πράξη, συν μια γραμμή `host all all <nodes-subnet>/24`· γραμμές ειδικές για το site μπαίνουν στο `postgres.extra_pg_hba`. Και οι τέσσερις κωδικοί (postgres superuser, replicator, rewind, authentik DB) παράγονται μία φορά, καρφιτσώνονται στο state, και δεν τυπώνονται ποτέ.

Δύο λειτουργικά γεγονότα αξίζει να τα ξέρετε ακόμα και με αυτοματισμό: οι ζωντανές ρυθμίσεις του Patroni ανήκουν στο DCS μετά το bootstrap (αλλάζονται με `patronictl edit-config`, όχι επεξεργάζοντας το αρχείο), και το restart του `patroni.service` στον τρέχοντα **leader** είναι πραγματικό failover, όχι no-op.

Verify: το `patronictl list` δείχνει ακριβώς 1 Leader + 2 replicas, όλα running/streaming, **και** ο ρόλος και η βάση `authentik` υπάρχουν στον leader — η τοπολογία του cluster μπορεί να είναι τέλεια πράσινη ενώ το βήμα 4 απέτυχε, οπότε το verify ελέγχει και τα δύο.

### haproxy

Βήμα 5 του οδηγού. Ένα HAProxy ανά node ως καθαρός router συνδέσεων PostgreSQL — το Authentik θα μιλάει πάντα στο `127.0.0.1:5000` (leader) / `:5001` (replicas), και κάθε HAProxy ανακαλύπτει ανεξάρτητα τον leader μέσω `GET /primary` στο REST API του Patroni. Το akropolis ρυθμίζει τον μηχανισμό· δεν κωδικοποιεί ποτέ σκληρά την απάντηση.

Το config φέρει τις ρυθμίσεις hardened για WAN: `timeout client/server 1h` (μακρόχρονες συνδέσεις LISTEN του Django), TCP keepalives και προς τις δύο κατευθύνσεις, και `on-marked-down shutdown-sessions` ώστε το failover να σκοτώνει stale sessions αντί να τα αφήνει να κρέμονται. Η σελίδα στατιστικών στο `:9000` παίρνει ένα password που παράγεται μία φορά και καρφιτσώνεται στο state.

Η σημασιολογία reload ακολουθεί σκληρά μαθήματα: μια αλλαγή μόνο-cfg σε ένα container που τρέχει είναι ένα ζωντανό `docker kill --signal=HUP` (το push του αρχείου κάνει truncate επί τόπου, διατηρώντας το inode του bind-mount)· μια αλλαγή compose είναι `down && up -d`· ένα αμετάβλητο config είναι ρητό no-op. Ένα container σε κατάσταση crash-loop (`Restarting`) μετράει ως *μη* τρέχον και ακολουθεί το πλήρες μονοπάτι `down && up -d`.

Το config αρχείο στέλνεται με mode **0644** — όχι 0600, παρά το ότι φέρει το password στατιστικών. Η επίσημη εικόνα `haproxy` ρίχνει προνόμια στον ανεπιτήρητο χρήστη `haproxy` (UID 99) πριν διαβάσει το config της, οπότε ένα root-owned bind-mount 0600 είναι μη αναγνώσιμο μέσα στο container και μπαίνει σε crash-loop με "Permission denied". Οι nodes είναι single-purpose hosts· η αναγνωσιμότητα on-host του password στατιστικών είναι το αποδεκτό trade-off (ταυτόσημο με το πραγματικό mode αρχείου της production ανάπτυξης). Τα pushes αρχείων επίσης συγκλίνουν το mode σε κάθε run, οπότε ένα αρχείο που έμεινε στο 0600 από παλιότερη έκδοση επιδιορθώνεται ακόμα κι όταν το περιεχόμενό του είναι αμετάβλητο.

Verify, δύο επίπεδα: το CSV στατιστικών σε **κάθε** node πρέπει να συγκλίνει σε ακριβώς 1 UP server στο `pg_primary_backend` και 2 UP στο `pg_replica_backend`· μετά ένα πραγματικό `SELECT 1` ως χρήστης `authentik` μέσω `127.0.0.1:5000` αποδεικνύει ολόκληρη την αλυσίδα — routing του HAProxy, leader του Patroni, pg_hba, credentials.

### tls

Αφαίρεση της λεπτομέρειας του provider πιστοποιητικών. Το μόνο που αλλάζει είναι το πώς προκύπτουν τα `fullchain.pem` + `privkey.pem`· η διανομή στο `/opt/nginx/certs` σε όλους τους nodes (privkey mode 0600) και η επαλήθευση είναι ίδιες για όλους τους providers.

- **`none`** — μόνο για testing/τοπική ανάπτυξη· απορρίπτεται για `production` sites κατά τη φόρτωση config. Το nginx θα σερβίρει plain HTTP στο :80. Σημειώστε τον ειλικρινή περιορισμό: χωρίς secure context, οι ροές WebAuthn/passkey δεν μπορούν να δοκιμαστούν με `none`.
- **`self_signed`** — ένα πιστοποιητικό 10 ετών παράγεται στον bootstrap leader με SANs που καλύπτουν το hostname, όλα τα ονόματα nodes, το VIP, και όλες τις IPs των nodes· μετά διανέμεται. Ιδεμποτικό: ένα υπάρχον πιστοποιητικό που ταιριάζει με το hostname και είναι έγκυρο για > 30 ημέρες διατηρείται.
- **`acme`** — οποιαδήποτε ACME CA μέσω `directory_url` (Let's Encrypt, HARICA ACME, ZeroSSL...). Αυτή η phase μόνο **προετοιμάζει**: το certbot εγκαθίσταται στον leader, το webroot ετοιμάζεται στο `/var/www/certbot`, και ένα self-signed placeholder μπαίνει στη θέση του ώστε το nginx να μπορεί να ξεκινήσει και να σερβίρει το HTTP-01 challenge. Η έκδοση, το multi-node deploy hook, και η αντικατάσταση του πιστοποιητικού συμβαίνουν αφού η phase του nginx το ανεβάσει. Ξεκινήστε με `staging: true`· αλλάξτε το μετά από ένα καθαρό end-to-end run για να αποφύγετε να κάψετε rate limits.
- **`import`** — εξωτερικά εκδοθέντα πιστοποιητικά, π.χ. η ροή του HARICA portal. Η επικύρωση γίνεται **στον σταθμό εργασίας πριν αγγίξει οτιδήποτε κάποιον node**: το private key πρέπει να ταιριάζει με το πιστοποιητικό (σύγκριση SPKI), το SAN πρέπει να καλύπτει το ρυθμισμένο hostname (κατανοούνται wildcards), και το πιστοποιητικό δεν πρέπει να έχει λήξει (≤ 30 ημέρες απομένουν είναι προειδοποίηση). Η ημερομηνία λήξης καρφιτσώνεται στο state ώστε το monitor να μπορεί να ειδοποιήσει γι' αυτήν — τα imported πιστοποιητικά δεν έχουν χρονόμετρο ανανέωσης, και το να προσποιούμαστε το αντίθετο θα ήταν χειρότερο από το να το πούμε ανοιχτά.

Verify: πιστοποιητικό και κλειδί παρόντα και κρυπτογραφικά ταιριαστά σε κάθε node.

### nginx-keepalived

Βήμα 6 του οδηγού, με τα μαθήματα του πεδίου εφαρμοσμένα πάνω στις αρχικές τιμές. Η σειρά επιβάλλεται, δεν ελπίζεται: το nginx ανεβαίνει σε όλους τους nodes και κάθε node επαληθεύεται **ξεχωριστά** πριν αγγιχτεί το keepalived.

Το **nginx** (`nginx:1.27-alpine`, host network) τερματίζει TLS και κάνει load balancing `least_conn` μεταξύ των τριών backends Authentik στο `:9443`. Το config είναι ίδιο σε κάθε node εκτός από το location `/monitor`, το οποίο επιστρέφει JSON ανά node (`{"node":"ak-node-2","ip":"..."}`) — οπότε το `curl -k https://<VIP>/monitor` λέει πάντα ποιος node κατέχει αυτή τη στιγμή το VIP· αυτό είναι επίσης αυτό που κάνει poll το εργαλείο monitoring. Το `:8080/nginx_status` σερβίρει `stub_status` περιορισμένο στο loopback, το υποδίκτυο των nodes, συν όποια CIDRs στο `network.stub_status_allow` — το `127.0.0.1` πρέπει να επιτρέπεται ρητά, αφού ένα `curl` on-node υπό host networking φτάνει από loopback, όχι από διεύθυνση υποδικτύου, και διαφορετικά θα απορριπτόταν με 403 από τον ίδιο του τον node. Η θύρα 80 ανακατευθύνει σε HTTPS με εξαίρεση για το path του ACME challenge (`alias`, όχι `root`). Με `tls.provider: none`, γίνεται render αντ' αυτού ένα plain HTTP proxy στο `:80`, το οποίο στοχεύει στον HTTP listener του Authentik (`:9080`) αντί για `:9443` — ο Go router παράγει απόλυτα URLs `https://` για οποιοδήποτε αίτημα φτάνει στον TLS listener του ανεξάρτητα από το `X-Forwarded-Proto`, κάτι που πίσω από ένα plain-HTTP frontend στέλνει το browser σε ένα ανύπαρκτο 443 και σπάει το UI (τα assets και ο flow executor αποτυγχάνουν με connection refused). Αλλαγές conf σε ένα container που τρέχει εφαρμόζονται με `down && up -d` — η παγίδα του inode single-file bind-mount κάνει το `nginx -s reload` αναξιόπιστο μετά από ένα push conf.

Το **keepalived** (bare-metal systemd) παρέχει το VIP μέσω VRRP με ένα track script `chk_nginx` (`nc -z localhost 443`, ή 80 για provider `none`): track weight **−25** — η τιμή που μαθεύτηκε στην πράξη και εξαλείφει ισοπαλίες προτεραιότητας σε σενάρια μονής αποτυχίας — με `fall 2 rise 2`, ώστε ένας node του οποίου το nginx πεθαίνει να χάνει το VIP μέσα σε ~6 δευτ. Η μη-προεμπτικότητα κωδικοποιείται στην κανονική μορφή του keepalived: **όλες οι instances `state BACKUP` + `nopreempt`** με διαφορετικές προτεραιότητες (`network.vrrp.priorities`, default `[100, 90, 80]`). Ο υγιής node με την υψηλότερη προτεραιότητα κερδίζει την αρχική εκλογή, και ένας ανακτημένος node δεν κλέβει ποτέ πίσω το VIP — ένα flap ανά αποτυχία, όχι δύο. Το `script_user root` + `enable_script_security` σιωπούν την παραβίαση script-security. Το `auth_pass` περικόπτεται σιωπηλά από το keepalived στους 8 χαρακτήρες, οπότε παράγονται και καρφιτσώνονται στο state ακριβώς 8.

Το **ACME finalization** τρέχει στο τέλος αυτής της phase όταν το έχει προετοιμάσει η phase tls: ένα αποκλειστικό root keypair (`/root/.ssh/id_ed25519_certbot`) παράγεται στον node του certbot και εξουσιοδοτείται με marker στους υπόλοιπους· το `certbot certonly --webroot` εκδίδει έναντι του ρυθμισμένου directory URL (ο κάτοχος του VIP σερβίρει το HTTP-01 challenge)· ένα deploy hook διαχειριζόμενο από akropolis στο `/etc/letsencrypt/renewal-hooks/deploy/akropolis-nginx.sh` διανέμει τα `fullchain.pem`/`privkey.pem` στους υπόλοιπους nodes και κάνει reload το nginx παντού σε **κάθε μελλοντική ανανέωση**· το hook τρέχει μία φορά αμέσως για να αντικαταστήσει το self-signed placeholder. Ο φάκελος πιστοποιητικών είναι directory mount, οπότε η αντικατάσταση αρχείου επί τόπου + reload είναι ασφαλής εκεί. Με `staging: true` το εκδοθέν πιστοποιητικό δεν είναι έμπιστο από browsers εξ ορισμού — αλλάξτε σε `false` και `--replay nginx-keepalived` για το πραγματικό. Μια αποτυχημένη έκδοση αφήνει το placeholder στη θέση του και σταματά τη phase· τίποτα δεν σπάει, διορθώστε DNS/προσβασιμότητα και κάντε replay.

Verify: το `/monitor` κάθε node επιστρέφει τη δική του ταυτότητα και το `stub_status` απαντά· **ακριβώς ένας** node κατέχει το VIP στο ρυθμισμένο interface· το ίδιο το VIP απαντά στο `/monitor`. Ένα ζωντανό test failover (σταματήστε το nginx στον κάτοχο του VIP, παρακολουθήστε το `/monitor` να αλλάζει ταυτότητα μέσα σε δευτερόλεπτα) αφήνεται σκόπιμα ως χειροκίνητη άσκηση — το akropolis δεν σκοτώνει υπηρεσίες για να αποδείξει κάτι.

### authentik

Βήμα 7 του οδηγού. Δύο μονοπάτια εκτέλεσης, επιλεγμένα εξετάζοντας τι πραγματικά τρέχει, ποτέ με υπόθεση:

**Bootstrap** (πρώτη ανάπτυξη): το `.env` και το compose γίνονται render σε όλους τους nodes, αλλά μόνο ο bootstrap leader ξεκινά — μόνος του — και η phase περιμένει και τα δύο containers του να φτάσουν σε Docker-`healthy`, ένα παράθυρο που καλύπτει το pull της εικόνας και το πλήρες run migration της βάσης (budget 15 λεπτά). Μόνο τότε ξεκινούν οι άλλοι δύο nodes, έναν-έναν, ο καθένας με health gate· βρίσκουν ένα migrated schema και ανεβαίνουν καθαρά. Τρεις nodes να τρέχουν migrations ταυτόχρονα πάνω στην ίδια βάση είναι ακριβώς η κατηγορία προβλήματος multi-node που αυτό το cluster έχει ήδη συναντήσει μία φορά — οπότε αποτρέπεται δομικά, δεν ελπίζεται να μην συμβεί.

**Rolling** (αλλαγή config ή tag σε cluster που τρέχει): εφαρμόζεται node-3 → node-2 → node-1 — το μοτίβο αντίστροφης σειράς αλλαγής που χρησιμοποιείται στο production — με `down && up -d` ανά node (ποτέ `restart`) και ένα health gate πριν προχωρήσει. Nodes με αμετάβλητο config παραλείπονται· ένας node που αποτυγχάνει στο gate σταματά τη phase ενώ οι υπόλοιποι nodes συνεχίζουν να σερβίρουν.

Το compose αρχείο αντικατοπτρίζει αυτούσιο το production: `depends_on: worker: condition: service_healthy` (το race condition dual-port-bind της 2026.5.0), healthchecks python3/urllib με `start_period: 60s` (η εικόνα αφαίρεσε το `curl` στη 2026.5.6), ο worker ως root με το docker socket για διαχείριση outposts, και `network_mode: host`. Mounts ειδικά ανά site (branding, locale patches) δεν είναι hardcoded — προστίθενται μέσω `authentik.extra_server_volumes` / `authentik.extra_worker_volumes`.

Το `.env` φέρει τα μη-διαπραγματεύσιμα αυτής της αρχιτεκτονικής: `AUTHENTIK_POSTGRESQL__HOST=127.0.0.1` (κάθε node μιλάει στο τοπικό του HAProxy, ποτέ σε μια IP node — το μάθημα ενός παλιότερου incident), και τα `AUTHENTIK_LISTEN__HTTP/HTTPS/METRICS` μετακινημένα στα 9080/9443/9300 επειδή το HAProxy κατέχει το 9000 και χωρίς τα overrides ο Go router αποτυγχάνει σιωπηλά να κάνει bind ενώ όλα φαίνονται ζωντανά. Το `AUTHENTIK_SECRET_KEY` (ίδιο παντού), το bootstrap password του akadmin, και το bootstrap API token παράγονται μία φορά και καρφιτσώνονται στο state· το token καταναλώνεται από το πρώτο migration του Authentik για να δημιουργήσει τα API credentials του akadmin, και αργότερα παραδίδεται στο monitor. Ο worker container επιπλέον κάνει override τα `AUTHENTIK_LISTEN__HTTP/METRICS` στα 9081/9301: κληρονομεί το κοινό `.env`, ο liveness server του κάνει bind σε αυτές τις διευθύνσεις, και — ξεκινώντας πριν τον server υπό host networking — διαφορετικά θα καταλάμβανε τα 9080/9300, αφήνοντας τα binds του server να αποτυγχάνουν σιωπηλά και το 9080 να απαντά με άδεια 200 από το liveness endpoint του worker σε κάθε path.

**Branding** (`authentik.branding.logo` / `.favicon` / `.background`): paths στον σταθμό εργασίας. Το akropolis τα ανεβάζει μέσω SFTP στο `/opt/authentik/branding/{icons,images}/` σε κάθε node και παράγει τα bind-mounts πάνω από `/web/dist/assets/{icons,images}/<name>`, ταιριάζοντας με το production compose. Το να γίνει μόνο το μισό αυτού — μια εγγραφή `extra_server_volumes` που δείχνει σε ένα αρχείο που κανείς δεν αντέγραψε — κάνει mount έναν φάκελο πάνω από ένα asset που λείπει και το σπάει, οπότε και τα δύο μισά προκύπτουν από μία ρύθμιση. Τα uploads συγκρίνονται με checksum, οπότε τα re-runs ούτε ξανα-μεταφέρουν ούτε προκαλούν churn στο compose stack.

Το mounting είναι μόνο το μισό. Το Authentik συνεχίζει να δείχνει το stock λογότυπο μέχρι η **εγγραφή brand** να δείχνει στο asset, οπότε σε ένα φρέσκο cluster μια απόλυτα σωστή ρύθμιση branding φαίνεται σπασμένη — το αρχείο είναι σε κάθε node, η σελίδα login είναι αμετάβλητη. Αφού το cluster είναι υγιές, το akropolis επομένως κάνει PATCH το default brand στο `/static/dist/assets/{icons,images}/<name>` (το μόνο απόλυτο prefix που δέχονται τα πεδία brand). Η phase restore το ξανα-εφαρμόζει επίσης, αφού ένα dump φέρνει τη δική του γραμμή brand, η οποία μπορεί να δείχνει σε assets που αυτό το cluster δεν είχε ποτέ. Μια αποτυχία εδώ προειδοποιεί και σας λέει να το ρυθμίσετε στο System > Brands — δεν αποτυγχάνει ποτέ τη phase, επειδή το ίδιο το cluster είναι εντάξει.

**SMTP email** (`AUTHENTIK_EMAIL__*`): το production `.env` φέρει ένα block mail — χωρίς αυτό η ανάκτηση password και τα email stages δεν μπορούν να στείλουν σιωπηλά. Σειρά επίλυσης ανά τιμή: `authentik.email` στο config του site, αλλιώς μια διαδραστική ερώτηση κατά την ώρα apply της οποίας η απάντηση καρφιτσώνεται στο state (ένα `--replay` δεν ρωτάει ποτέ ξανά και κάνει render το ίδιο αρχείο). Το password SMTP δεν γράφεται ποτέ στο αρχείο config: ζητείται με κρυφή είσοδο μία φορά και καρφιτσώνεται στο state (0600). Ορίστε `authentik.email.enabled: false` για να μην έχετε block και ερωτήσεις.

Verify: και τα δύο containers `healthy` σε κάθε node, το `/-/health/ready/` απαντά ανά node, και το API απαντά στο καρφιτσωμένο bootstrap token — αποδεικνύοντας ήδη το ακριβές credential από το οποίο θα εξαρτηθεί αργότερα το monitor.

### restore *(προαιρετική)*

Η κίνηση migration/cutover, ενσωματωμένη ως phase μεταξύ authentik και handoff: ορίστε `restore.sql_file` (απλό `.sql` ή `.sql.gz`) και η μόλις-bootstrap-αρισμένη βάση αντικαθίσταται από ένα `pg_dump` του παλιού συστήματος — πραγματικοί χρήστες, providers και flows αντί για μια κενή εγκατάσταση akadmin. Χωρίς `restore.sql_file` η phase καταγράφει "skipped" και το pipeline τρέχει χωρίς να την αγγίξει.

Όταν είναι ενεργή είναι καταστροφική εξ ορισμού, οπότε η σειρά είναι αυστηρή και κάθε βήμα έχει gate: εντοπισμός του **τρέχοντος** leader Patroni μέσω REST `/primary` (μπορεί να μην είναι πια ο node-1)· έλεγχος της κεφαλίδας `SET <guc>` του dump έναντι των `pg_settings` του server-στόχου **πριν από οτιδήποτε καταστροφικό** — ένα dump γραμμένο από νεότερο `pg_dump` φέρει GUCs που ένας παλιότερος server απορρίπτει (το `pg_dump` 17 σε PostgreSQL 16 εκδίδει `SET transaction_timeout = 0;`), και η ανακάλυψη αυτού μετά το DROP θα άφηνε το cluster κάτω πάνω σε άδεια βάση· αυτές οι συγκεκριμένες γραμμές κεφαλίδας αφαιρούνται κατά τη φόρτωση και τίποτα άλλο δεν φιλτράρεται, οπότε το `ON_ERROR_STOP` συνεχίζει να διέπει το πραγματικό περιεχόμενο· σταμάτημα του authentik σε **όλους** τους nodes πριν αγγιχτεί η βάση· SFTP του dump στον leader (το push σε base64 είναι αχρησιμοποίητο σε μεγέθη dump) και επαλήθευση sha256 από άκρη σε άκρη· `DROP DATABASE ... WITH (FORCE)` → `CREATE ... OWNER authentik` → `psql -v ON_ERROR_STOP=1` — οποιοδήποτε σφάλμα σταματά τη phase με το authentik σκόπιμα ακόμα κάτω, ποτέ μισό-πάνω σε μισά-δεδομένα· διαγραφή του dump από τον node (περιέχει κάθε μυστικό που κατέχει το IdP)· μετά επαναφορά του authentik: ο **worker ξεκινά μόνος** (`up -d --no-deps worker`) και έχει gate στο `restore.migration_timeout` (default 3600 δευτ.). Αυτό έχει σημασία — μια restored βάση δεν είναι φρέσκια, και ο worker που κάνει migrate πραγματικά δεδομένα ξεπερνά τη δική της αναμονή εξάρτησης του compose (`start_period` 60s + `interval` 30s × `retries` 3 ≈ 150s), μετά την οποία το `docker compose up -d` ματαιώνει τα πάντα με *dependency failed to start* ενώ το migration τρέχει τέλεια. Ο server ακολουθεί μόλις ο worker γίνει healthy, μετά οι άλλοι nodes έναν-έναν — οι workers τους βρίσκουν ένα migrated schema και ανεβαίνουν κανονικά. Όποτε λήγει ένα health gate, η ουρά του σχετικού log container τυπώνεται αυτόματα αντί να σας πει να πάτε να τη φέρετε. Το verify αποδεικνύει ότι το restored schema έχει πίνακες, ο `authentik_core_user` είναι γεμάτος, και κάθε node είναι healthy και ready. Το sha256 και το timestamp του dump καταλήγουν στο state ως το αποδεικτικό ίχνος· σε πραγματικό cutover, ξανατρέξτε με το φρέσκο dump μέσω `--replay restore`.

### handoff

Η τελευταία phase, και η μόνη που δεν αγγίζει τίποτα στους nodes. Γράφει το `config.yml` του εργαλείου monitoring **στον σταθμό εργασίας** (path από `monitor.output`, default `./config.<site>.monitor.yml`, mode 0600), γεμάτο εξ ολοκλήρου από το config του site και το καρφιτσωμένο state: τα groups nodes ανά υπηρεσία, οι θύρες, οι ρυθμίσεις συλλογής log μέσω SSH, τα credentials στατιστικών HAProxy και postgres, το API token του Authentik που η phase authentik έχει ήδη αποδείξει έναντι του ζωντανού API, και οι τιμές `track_weight` (−25) και `base_priority` ανά node ακριβώς όπως έχουν αναπτυχθεί — το monitor υπολογίζει τις ενεργές προτεραιότητες VRRP από αυτές, οπότε το config και η πραγματικότητα ταιριάζουν εκ κατασκευής και όχι από πειθαρχία. Όταν ο provider tls είναι `import`, η λήξη του πιστοποιητικού σημειώνεται στο εκδιδόμενο αρχείο, αφού δεν υπάρχει χρονόμετρο ανανέωσης.

Μετά τυπώνει την κάρτα προσγείωσης: το URL admin, το username `akadmin`, και το bootstrap password — εμφανίζονται **μία φορά**, στο terminal σας, επειδή χρειάζεστε αυτό για να συνδεθείτε· αλλάξτε το μετά την πρώτη σύνδεση. Συνθήκες pending-ACME και staging-cert αναφέρονται στην κάρτα αν ισχύουν.

Verify: κάνει parse το εκδιδόμενο αρχείο πίσω και επιβεβαιώνει το schema: όλα τα top-level keys που περιμένει το monitor, ένα μη-κενό API token, και 3 nodes σε κάθε group υπηρεσίας.

## Καθάρισμα ενός site

Το `akropolis clean config.<site>.yml` γκρεμίζει ολόκληρο το stack πίσω σε γυμνά VMs — η αντίστροφη διαδικασία του pipeline, για επαναληπτικό testing. Το teardown τρέχει σε αντίστροφη σειρά χτισίματος (keepalived/VIP πρώτα, ώστε τίποτα να μη δρομολογεί κίνηση σε ένα cluster που γκρεμίζεται· η βάση πριν το DCS της): keepalived → nginx → authentik → haproxy → patroni + **όλα** τα δεδομένα PostgreSQL → etcd + δεδομένα → υλικό TLS (letsencrypt, webroot, το distribution key του certbot και η γραμμή του στο authorized_keys) → reset UFW με το ssh ξανα-επιτρεπόμενο *πριν* το re-enable → το block στο `/etc/hosts` → υπολείμματα restore-dump στο `/tmp`. Τα πακέτα (docker, postgresql-16, keepalived, certbot) και το hostname αφήνονται σκόπιμα ανέγγιχτα — η κατάσταση apt ανήκει στην πολιτική patching σας, και η αφαίρεση δεδομένων και config είναι αυτό που κάνει το επόμενο `provision` ειλικρινές.

Η καταστροφή κερδίζει το gate πληκτρολόγησης-ονόματος-site σε *κάθε* περιβάλλον, όχι μόνο σε production· το `site.environment: production` απορρίπτεται εντελώς εκτός αν δοθεί επίσης το `--i-know-this-is-production`. Το τοπικό state file αρχειοθετείται στο `.state/<site>.json.cleaned-<timestamp>` (0600 — τα καρφιτσωμένα μυστικά είναι το αποδεικτικό σας ίχνος) και αφαιρείται, ώστε το επόμενο provision να ξαναδημιουργήσει κάθε μυστικό και το `refuse_existing` του preflight να περνάει σε μια πραγματικά κενή κατάσταση. Το καθάρισμα ενός μισο-χτισμένου node — ακριβώς αυτό που αφήνει πίσω ένα αποτυχημένο provision — είναι υποστηριζόμενη περίπτωση: κάθε βήμα είναι ιδεμποτικό.

## Αναφορά configuration

Το `config.example.yml` είναι η αναφορά: κάθε key που διαβάζει το akropolis εμφανίζεται εκεί, σχολιασμένο (commented out) όταν είναι προαιρετικό. Αυτό επιβάλλεται αντί να υπόσχεται — το `python3 tools/audit_config_keys.py` αποτυγχάνει αν ο κώδικας διαβάζει ένα key που το example δεν αναφέρει ποτέ. Υπάρχει επειδή τα `base.apt_upgrade` και `network.trusted_proxies` ήταν τεκμηριωμένα σε αυτό το README και υλοποιημένα στον κώδικα αλλά έλειπαν από το example, πράγμα που σήμαινε ότι στην πράξη κανείς δεν μπορούσε να τα βρει.

## Πίσω από εξωτερικό reverse proxy

Αν το δημόσιο TLS τερματίζεται upstream — π.χ. μια instance Traefik που κρατά ένα wildcard πιστοποιητικό HARICA, με το cluster προσβάσιμο μόνο μέσω αυτής — η σκόπιμη ρύθμιση είναι `tls.provider: self_signed`: το εσωτερικό VIP σερβίρει ένα self-signed πιστοποιητικό, και το εξωτερικό proxy προωθεί σε αυτό με απενεργοποιημένη την επαλήθευση πιστοποιητικού. Ο έλεγχος DNS→VIP του preflight είναι μόνο-προειδοποίηση για `self_signed`, οπότε ένα δημόσιο hostname που επιλύεται στο proxy αντί για το VIP δεν μπλοκάρει.

Το κομμάτι που είναι εύκολο να πάει σιωπηλά στραβά σε αυτή την τοπολογία είναι η ταυτότητα client: χωρίς ειδικό χειρισμό, το nginx του cluster αντικαθιστά το `X-Real-IP` με τη διεύθυνση του proxy και το `X-Forwarded-Proto` με το δικό του scheme, οπότε το Authentik βλέπει κάθε login σαν να έρχεται από το proxy — δηλητηριάζοντας πολιτικές reputation, event logs, και GeoIP. Δηλώστε το proxy αντ' αυτού:

```yaml
network:
  trusted_proxies:
    - 192.168.20.20        # IPs ή CIDRs του upstream proxy
```

Όταν οριστεί, το nginx εμπιστεύεται το `X-Forwarded-For` από αυτές τις διευθύνσεις (`set_real_ip_from` + `real_ip_recursive`), οπότε οι πραγματικές IPs client φτάνουν στο Authentik, και το αρχικό `X-Forwarded-Proto` περνάει αντί να αντικαθίσταται (πέφτοντας πίσω στο τοπικό scheme όταν η κεφαλίδα λείπει, ώστε η απευθείας εσωτερική πρόσβαση να συνεχίζει να δουλεύει). Καταχωρίστε μόνο διευθύνσεις που πραγματικά ελέγχετε — ένα trusted proxy μπορεί να ισχυριστεί οποιαδήποτε IP client του αρέσει.

## State file & μυστικά

Κάθε site παίρνει ένα JSON state file (default `.state/<site>.json`, mode 0600) που καταγράφει την ολοκλήρωση phases και **καρφιτσωμένες τιμές γεννιούνται-μία-φορά**: το initial-cluster token του etcd, όλα τα passwords PostgreSQL, το password στατιστικών του HAProxy, metadata TLS. Το pinning είναι αυτό που κάνει τα re-runs ασφαλή — ένα ολοκληρωμένο bootstrap δεν μπορεί ποτέ να ξανα-bootstrap-αριστεί, και ένα re-render δεν μπορεί ποτέ να περιστρέψει ένα password κάτω από ένα cluster που τρέχει. Το state file αρνείται να φορτωθεί για διαφορετικό όνομα site από αυτό για το οποίο δημιουργήθηκε.

Στάση ασφαλείας, ειπωμένη ξεκάθαρα:

- Το αρχείο config του site δεν περιέχει **κανένα μυστικό** και είναι ασφαλές να γίνει commit (git-ignored εξ ορισμού ούτως ή άλλως, εκτός από το example).
- Τα passwords SSH, όταν χρησιμοποιούνται, ζητούνται κατά την εκτέλεση και δεν αποθηκεύονται ποτέ.
- Τα παραγόμενα μυστικά ζουν αυτή τη στιγμή **σε καθαρό κείμενο** μέσα στο state file. Αυτό είναι αποδεκτό για δουλειά lab και είναι σημειωμένο ως σκληρή απαίτηση προς διόρθωση (κρυπτογράφηση age/sops ή OS keyring) πριν από χρήση production. Μεταχειριστείτε το `.state/` αναλόγως: είναι git-ignored, κρατήστε το έτσι.
- Εισαγόμενα private keys TLS περνούν από τη μνήμη του σταθμού εργασίας κατά την επικύρωση και διανομή· γράφονται μόνο στο `/opt/nginx/certs/privkey.pem` (0600) στους nodes.

## Roadmap

handoff (έκδοση του config του monitor, εκτύπωση του URL admin) → ενσωμάτωση του monitoring TUI ως `akropolis monitor` → κρυπτογραφημένα μυστικά state → ελληνική έκδοση αυτού του οδηγού.

## Άδεια χρήσης

MIT.
