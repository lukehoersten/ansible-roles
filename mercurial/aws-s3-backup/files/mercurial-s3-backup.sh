#!/bin/bash

BUCKET=$1
DATE=`date --iso-8601`
BACKUP_DIR=$2
BACKUP_TAR="/tmp/$BUCKET-$DATE.tgz"

tar -zc -f $BACKUP_TAR $BACKUP_DIR
aws s3 mb "s3://$BUCKET/"
aws s3 cp $BACKUP_TAR "s3://$BUCKET/"
