#!/bin/bash

# install system deps
sudo yum install -y postgresql git python3 python3-wheel python3-pip gcc

# install pip deps
sudo pip3 install kaggle pandas psycopg2-binary

# write kaggle creds
mkdir /home/ec2-user/.kaggle
touch /home/ec2-user/.kaggle/kaggle.json
echo ${kaggle_credentials} > /home/ec2-user/.kaggle/kaggle.json
export KAGGLE_CONFIG_DIR='/home/ec2-user/.kaggle'

# download data
cd /home/ec2-user
kaggle datasets download -d rounakbanik/the-movies-dataset
unzip the-movies-dataset.zip

# clone the repo
git clone https://github.com/Nickhil-Sethi/stat706-class-materials.git

# export database variables
export DB_USER=${DB_USER}
export DB_PASS=${DB_PASS}
export DB_HOST=${DB_HOST}
export DB_PORT=${DB_PORT}
