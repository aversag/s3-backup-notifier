.DEFAULT_GOAL := help

help:
	@echo "${PROJECT}"
	@echo "${DESCRIPTION}"
	@echo ""
	@echo "	layer - prepare the layer"
	@echo "	package - prepare the package"
	@echo "	deploy - deploy the lambda function"
	@echo "	clean - clean the build folder"
	@echo "	clean-layer - clean the layer folder"
	@echo "	cleaning - clean build and layer folders"

################## Project ####################
PROJECT ?= s3monitoring
DESCRIPTION := S3 Backup Notifier
###############################################

################## Variables ##################
S3_BUCKET ?= ${PROJECT}-artifacts
S3_PREFIX := MyPrefix
BUCKETS_BLACKLIST := backup.bucket
RECIPIENTS := david@doe.com
SENDER := john@doe.com
AWS_REGION ?= eu-west-3
AWS_SES_REGION ?= eu-west-1
ENV ?= dev
###############################################

package: clean
	@echo "Consolidating python code in ./build"
	mkdir -p build
	mkdir -p sam-template

	cp -R ./python/*.py ./build/

	@echo "zipping python code, uploading to S3 bucket, and transforming template"
	aws cloudformation package \
			--template-file template.yml \
			--s3-bucket ${S3_BUCKET} \
			--output-template-file ./sam-template/sam.yml

	@echo "Copying updated cloud template to S3 bucket"
	aws s3 cp ./sam-template/sam.yml 's3://${S3_BUCKET}/'

deploy:
	aws cloudformation deploy \
			--template-file ./sam-template/sam.yml \
			--region ${AWS_REGION} \
			--stack-name "${PROJECT}-${ENV}" \
			--capabilities CAPABILITY_IAM \
			--parameter-overrides \
				ENV=${ENV} \
				BUCKETSBLACKLIST=${BUCKETS_BLACKLIST} \
				S3PREFIX=${S3_PREFIX} \
				PROJECT=${PROJECT} \
				RECIPIENTS=${RECIPIENTS} \
				SENDER=${SENDER} \
				AWSREGION=${AWS_REGION} \
				AWSSESREGION=${AWS_SES_REGION} \
				BOTOLAYER=${BOTOLAYER} \
				BOTOLAYERVERSION=${BOTOLAYERVERSION} \
			--no-fail-on-empty-changeset

layer: clean-layer
	mkdir -p layer
	pip3 install \
		--isolated \
		--disable-pip-version-check \
		-Ur ./python/requirements.txt -t ./layer/
	zip -r boto3-layer.zip layer
	aws lambda publish-layer-version --layer-name boto3 --zip-file fileb://boto3-layer.zip

clean-layer:
	@rm -fr layer/
	@rm -fr dist/
	@rm -fr htmlcov/
	@rm -fr site/
	@rm -fr .eggs/
	@rm -fr .tox/
	@find . -name '*.egg-info' -exec rm -fr {} +
	@find . -name '.DS_Store' -exec rm -fr {} +
	@find . -name '*.egg' -exec rm -f {} +
	@find . -name '*.pyc' -exec rm -f {} +
	@find . -name '*.pyo' -exec rm -f {} +
	@find . -name '*~' -exec rm -f {} +
	@find . -name '__pycache__' -exec rm -fr {} +

clean:
	@rm -fr build/
	@rm -fr dist/
	@rm -fr htmlcov/
	@rm -fr site/
	@rm -fr .eggs/
	@rm -fr .tox/
	@find . -name '*.egg-info' -exec rm -fr {} +
	@find . -name '.DS_Store' -exec rm -fr {} +
	@find . -name '*.egg' -exec rm -f {} +
	@find . -name '*.pyc' -exec rm -f {} +
	@find . -name '*.pyo' -exec rm -f {} +
	@find . -name '*~' -exec rm -f {} +
	@find . -name '__pycache__' -exec rm -fr {} +

cleaning: clean clean-layer

tear-down:
	@read -p "Are you sure that you want to destroy stack '${PROJECT}-${ENV}'? [y/N]: " sure && [ $${sure:-N} = 'y' ]
	aws cloudformation delete-stack --stack-name "${PROJECT}-${ENV}"
