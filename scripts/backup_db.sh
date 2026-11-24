#!/bin/bash
BACKUP_DIR=~/db_backups
DB_PATH=~/CoinMate-Backend/coin_mate.db
DATENOW=$(date +%Y%m%d_%H%M)
mkdir -p $BACKUP_DIR
cp $DB_PATH $BACKUP_DIR/coin_mate_$DATENOW.db
find $BACKUP_DIR -name '*.db' -mtime +7 -delete
echo "Backup done: coin_mate_$DATENOW.db"
