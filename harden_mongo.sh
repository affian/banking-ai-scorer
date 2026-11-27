#!/bin/bash
# harden_mongo.sh - turn on auth + TLS for the bankdemo MongoDB, run ON the VM.
#   bash ~/harden_mongo.sh <VM_PUBLIC_IP>
# The argument is the PUBLIC IP clients connect to (it goes into the certificate).
# Writes ~/mongo_creds.txt (mode 600) with the two generated passwords.
# Safe to re-run: it restores the original config first.
set -e
PUBIP="${1:?usage: bash harden_mongo.sh <public-ip>}"
LP=$(openssl rand -hex 16)
AP=$(openssl rand -hex 16)

# 0. Start from the original config with mongod running (auth and TLS off)
[ -f /etc/mongod.conf.orig ] || sudo cp /etc/mongod.conf /etc/mongod.conf.orig
sudo cp /etc/mongod.conf.orig /etc/mongod.conf
sudo systemctl restart mongod
sleep 5
systemctl is-active --quiet mongod || { echo "mongod will not start on the ORIGINAL config"; sudo journalctl -u mongod -n 10 --no-pager; exit 1; }

# 1. Users, then save the passwords straight away
mongosh --quiet --eval '
db = db.getSiblingDB("bankdemo");
["bankloader","bankapp"].forEach(u => { try { db.dropUser(u) } catch (e) {} });
db.createUser({user:"bankloader", pwd:"'"$LP"'", roles:[{role:"readWrite", db:"bankdemo"}]});
db.createUser({user:"bankapp",    pwd:"'"$AP"'", roles:[{role:"read",      db:"bankdemo"}]});
print("users ready")'
( umask 077; { echo "BANKLOADER_PASSWORD=$LP"; echo "BANKAPP_PASSWORD=$AP"; } > ~/mongo_creds.txt )
echo "credentials saved to ~/mongo_creds.txt"

# 2. Certificate: private CA + server cert whose SAN is the public IP
sudo mkdir -p /etc/ssl/mongo
sudo chmod 755 /etc/ssl/mongo
cd /etc/ssl/mongo
sudo openssl req -x509 -newkey rsa:4096 -nodes -keyout ca.key -out ca.pem -days 825 -subj "/CN=bankdemo-ca" 2>/dev/null
sudo openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=$PUBIP" 2>/dev/null
echo "subjectAltName=IP:$PUBIP" | sudo tee san.ext >/dev/null
sudo openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out server.crt -days 825 -extfile san.ext 2>/dev/null
sudo sh -c 'cat server.key server.crt > server.pem'
sudo chown mongod:mongod server.pem ca.pem
sudo chmod 600 server.pem ca.key
sudo chmod 644 ca.pem
sudo restorecon -R /etc/ssl/mongo 2>/dev/null || true

# 3. mongod.conf. MongoDB 7 needs CAFile with TLS; allowConnectionsWithoutCertificates
#    keeps it from demanding a client certificate (clients only verify the server).
sudo python3 - <<'PY'
import re
p = "/etc/mongod.conf"
s = open(p).read()
s = re.sub(r"(?ms)^net:.*?(?=^\S|\Z)", "", s)
s = re.sub(r"(?ms)^security:.*?(?=^\S|\Z)", "", s)
s = s.rstrip() + """

net:
  port: 27017
  bindIp: 0.0.0.0
  tls:
    mode: requireTLS
    certificateKeyFile: /etc/ssl/mongo/server.pem
    CAFile: /etc/ssl/mongo/ca.pem
    allowConnectionsWithoutCertificates: true

security:
  authorization: enabled
"""
open(p, "w").write(s)
PY
sudo systemctl restart mongod
sleep 5
if ! systemctl is-active --quiet mongod; then
  echo "mongod FAILED with TLS+auth config. Restoring original config."
  sudo journalctl -u mongod -n 8 --no-pager
  sudo cp /etc/mongod.conf.orig /etc/mongod.conf && sudo systemctl restart mongod
  exit 1
fi
echo "mongod: active"

# 4. Firewall
sudo firewall-cmd --permanent --add-port=27017/tcp >/dev/null && sudo firewall-cmd --reload >/dev/null

# 5. Self-test on the VM: TLS + auth + read-only user
mongosh --quiet --tls --tlsCAFile /etc/ssl/mongo/ca.pem --tlsAllowInvalidHostnames \
  "mongodb://bankapp:$AP@127.0.0.1:27017/bankdemo?authSource=bankdemo" \
  --eval 'print("bankapp reads: accounts=" + db.accounts.countDocuments())'
echo "listening:"; ss -ltn | grep 27017
