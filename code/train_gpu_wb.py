import sys
import os
import pandas as pd
import numpy as np
import torch
import torch.utils.data as du
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import util
from util import *
#from network import sparseGO_nn
from network_dropout2 import sparseGO_nn
import argparse
import time
import wandb

def train_model(run,config,model, optimizer, criterion, train_data, cell_features, drug_features, batch_size, model_dir, cuda_id, num_epochs,decay_rate):
     print('\nTraining started...')

     since = time.time()

     # initialize metrics
     max_corr_pearson = -1
     max_corr_spearman = -1
     min_loss = 1000

    # data for train and validation
     train_feature, train_label, val_feature, val_label = train_data

     # !! Modify output/labels to make small AUCs important
     # train_label = torch.log(train_label+10e-4)
     # val_label = torch.log(val_label+10e-4)

     # data to GPU
     train_label_gpu = torch.autograd.Variable(train_label.cuda(cuda_id))
     val_label_gpu = torch.autograd.Variable(val_label.cuda(cuda_id), requires_grad=False)
     print("\nTraining samples: ",train_label_gpu.shape[0])
     print("Val samples: ",val_label_gpu.shape[0])
     print('-----------------')

     train_loader = du.DataLoader(du.TensorDataset(train_feature,train_label), batch_size=batch_size, shuffle=False)
     val_loader = du.DataLoader(du.TensorDataset(val_feature,val_label), batch_size=batch_size, shuffle=False)

     optimizer.zero_grad()
     lr0 = optimizer.param_groups[0]['lr'] # extract learning rate

     for epoch in range(config.epochs):
         epoch_start_time = time.time()
         train_cum_loss = 0

         model.train() # Tells the model that you are training it. So effectively layers which behave different on the train and test procedures know what is going on

         train_predict = torch.zeros(0,1).cuda(cuda_id) # initialize training results tensor

         # Learning rate decay
         optimizer.param_groups[0]['lr'] = lr0*(1/(1+decay_rate*epoch))

         # Training epoch
         for i, (inputdata, labels) in enumerate(train_loader):
             # Convert torch tensor to Variable
             features = build_input_vector(inputdata, cell_features, drug_features)

             cuda_features = torch.autograd.Variable(features.cuda(cuda_id))
             cuda_labels = torch.autograd.Variable(labels.cuda(cuda_id))

             # Forward pass & statistics
             out = model(cuda_features)
             train_predict = torch.cat([train_predict, out])

             loss = criterion(out, cuda_labels)
             train_cum_loss += float(loss.item())

             # Backwards pass & update
             optimizer.zero_grad() # zeroes the grad attribute of all the parameters passed to the optimizer
             loss.backward() # Computes the sum of gradients of given tensors with respect to graph leaves.
             optimizer.step() # Performs a parameter update based on the current gradient
             torch.cuda.empty_cache()

         train_corr = pearson_corr(train_predict, train_label_gpu) # compute pearson correlation
         train_corr_spearman = spearman_corr(train_predict.cpu().detach().numpy(), train_label_gpu.cpu()) # compute spearman correlation

         print('Epoch %d' % (epoch + 1))
         print("L.r. ", round(optimizer.param_groups[0]['lr'],6))
         print('Training Loss: {:.4f}; Training Corr (pear.): {:.6f}'.format(train_cum_loss, train_corr))
         print('Training Corr (sp.): {:.6f}'.format(train_corr_spearman))
         print('Allocated after train:', round(torch.cuda.memory_allocated(0)/1024**3,3), 'GB')
         #print('Cached after train:   ', round(torch.cuda.memory_reserved(0)/1024**3,3), 'GB')
         print("    ")

         val_cum_loss = 0

         #torch.save(model, model_dir + '/model_' + str(epoch+1) + '.pt')

         #Validation: random variables in training mode become static
         model.eval()

         val_predict = torch.zeros(0,1).cuda(cuda_id)

         # Val epoch
         with torch.no_grad():
             for i, (inputdata, labels) in enumerate(val_loader):
                  # Convert torch tensor to Variable
                  features = build_input_vector(inputdata, cell_features, drug_features)

                  cuda_features = torch.autograd.Variable(features.cuda(cuda_id))
                  cuda_labels = torch.autograd.Variable(labels.cuda(cuda_id))

                  # Forward pass & statistics
                  out = model(cuda_features)
                  val_predict = torch.cat([val_predict, out]) # concatenate predictions
                  torch.cuda.empty_cache()

         val_cum_loss = criterion(val_predict, val_label_gpu)
         val_corr = pearson_corr(val_predict, val_label_gpu) # compute correlation
         val_corr_spearman = spearman_corr(val_predict.cpu().detach().numpy(), val_label_gpu.cpu())

         print('Val Loss: {:.4f}; Val Corr (pear.): {:.6f}'.format(val_cum_loss, val_corr))
         print('Val Corr (sp.): {:.6f}'.format(val_corr_spearman))
         print('Allocated after val:', round(torch.cuda.memory_allocated(0)/1024**3,3), 'GB')
         #print('Cached after val:   ', round(torch.cuda.memory_reserved(0)/1024**3,3), 'GB')

         epoch_time_elapsed = time.time() - epoch_start_time
         print('Epoch time: {:.0f}m {:.0f}s'.format(
         epoch_time_elapsed // 60, epoch_time_elapsed % 60))

         print('-----------------')

         wandb.log({"pearson_val": val_corr,
                    "spearman_val": val_corr_spearman,
                    "loss_val": val_cum_loss
                    })

         # checkpoint = {
         #            'epoch': epoch+1,
         #            'state_dict': model.state_dict(),
         #            'optimizer': optimizer.state_dict()
         #            }
         # save_ckp(checkpoint, False, model_dir, model_dir)

         if val_corr >= max_corr_pearson:
             max_corr_pearson = val_corr
             best_model_p = epoch+1
             print("pearson: ",epoch+1)
             torch.save(model, model_dir + '/best_model_p.pt')

         if val_corr_spearman >= max_corr_spearman:
             max_corr_spearman = val_corr_spearman
             best_model_s = epoch+1
             print("spearman: ",epoch+1)
             torch.save(model, model_dir + '/best_model_s.pt')

         if val_cum_loss <= min_loss:
             min_loss = val_cum_loss
             best_model_l = epoch+1
             print("loss: ",epoch+1)
             torch.save(model, model_dir + '/best_model_l.pt')

     wandb.log({"max_pearson_val": max_corr_pearson,
                "max_spearman_val": max_corr_spearman,
                "min_loss_val": min_loss,
                })

     # torch.save(model, model_dir + '/last_model.pt')

     print("Best performed model (loss) (epoch)\t%d" % best_model_l,'loss: {:.6f}'.format(min_loss))

     print("Best performed model (pearson) (epoch)\t%d" % best_model_p, 'corr: {:.6f}'.format(max_corr_pearson))

     print("Best performed model (spearman) (epoch)\t%d" % best_model_s,'corr: {:.6f}'.format(max_corr_spearman))

     # artifact = wandb.Artifact("Last_model",type="model")
     # artifact.add_file(model_dir + '/last_model.pt')
     # run.log_artifact(artifact)

     artifact = wandb.Artifact("Loss_model",type="model")
     artifact.add_file(model_dir + '/best_model_l.pt')
     run.log_artifact(artifact)

     artifact = wandb.Artifact("Pearson_model",type="model")
     artifact.add_file(model_dir + '/best_model_p.pt')
     run.log_artifact(artifact)

     artifact = wandb.Artifact("Spearman_model",type="model")
     artifact.add_file(model_dir + '/best_model_s.pt')
     run.log_artifact(artifact)


     time_elapsed = time.time() - since
     print('\nTraining complete in {:.0f}m {:.0f}s'.format(
         time_elapsed // 60, time_elapsed % 60))


def predict(statistic,run,criterion,predict_data, gene_dim, drug_dim, model_file, batch_size, result_file, cell_features, drug_features, CUDA_ID):

    feature_dim = gene_dim + drug_dim

    model = torch.load(model_file, map_location='cuda:%d' % CUDA_ID)

    predict_feature, predict_label = predict_data
    # !! Modify output/labels to make small AUCs important
    # predict_label = torch.log(predict_label+10e-4)

    predict_label_gpu = predict_label.cuda(CUDA_ID)

    model.cuda(CUDA_ID)
    model.eval()

    test_loader = du.DataLoader(du.TensorDataset(predict_feature,predict_label), batch_size=batch_size, shuffle=False)
    test_cum_loss = 0
    #Test
    test_predict = torch.zeros(0,1).cuda(CUDA_ID)
    with torch.no_grad():
        for i, (inputdata, labels) in enumerate(test_loader):
            # Convert torch tensor to Variable
            features = build_input_vector(inputdata, cell_features, drug_features)

            cuda_features = Variable(features.cuda(CUDA_ID), requires_grad=False)
            cuda_labels = torch.autograd.Variable(labels.cuda(CUDA_ID))

            # make prediction for test data
            out = model(cuda_features)
            test_predict = torch.cat([test_predict, out])

    test_cum_loss=criterion(test_predict, predict_label_gpu)
    test_corr = pearson_corr(test_predict, predict_label_gpu)
    test_corr_spearman = spearman_corr(test_predict.cpu().detach().numpy(), predict_label_gpu.cpu())

    print('Test loss: {:.4f}'.format(test_cum_loss))
    print('Test Corr (p): {:.4f}'.format(test_corr))
    print('Test Corr (s): {:.4f}'.format(test_corr_spearman))

    print('Allocated:', round(torch.cuda.memory_allocated(0)/1024**3,1), 'GB')
#    print('Cached:   ', round(torch.cuda.memory_cached(0)/1024**3,1), 'GB')

    wandb.log({"pearson_test_"+statistic: test_corr,
               "spearman_test_"+statistic: test_corr_spearman,
               "loss_test_"+statistic: test_cum_loss})

    np.savetxt(result_file+statistic+'_test_predictions.txt', test_predict.cpu().detach().numpy(),'%.5e')
    artifact2 = wandb.Artifact(statistic+"_predictions",type="predictions")
    artifact2.add_file(result_file+statistic+'_test_predictions.txt')
    run.log_artifact(artifact2)

since0 = time.time()
parser = argparse.ArgumentParser(description='Train sparseGO')
#inputdir="/Users/ksada/OneDrive - Tecnun/SparseGO_code/data/cross_validation/samples1/" # CHANGE
inputdir="/Users/ksada/OneDrive - Tecnun/SparseGO_code/data/toy_example/" # CHANGE
#inputdir="../data/" # CHANGE

modeldir="../results/prueba_spyder_wb" # PUEDO CAMBIAR EL NOMBRE PARA GUARDAR EN OTRO SITIO
ontology = "drugcell_ont.txt"
#ontology = "ontology.txt"
#mutation = "cell2mutation_clinical.txt"
mutation = "cell2mutation.txt"
#ontology = "lincs_ont.txt"
#ontology = "lincs_ont_7.txt"
#mutation = "cell2expression_clinical.txt"
#mutation = "cell2expression.txt"

parser.add_argument('-onto', help='Ontology file used to guide the neural network', type=str, default=inputdir+ontology)
parser.add_argument('-train', help='Training dataset', type=str, default=inputdir+"drugcell_train.txt")
parser.add_argument('-val', help='Validation dataset', type=str, default=inputdir+"drugcell_val.txt")
parser.add_argument('-epoch', help='Training epochs for training', type=int, default=20)
parser.add_argument('-lr', help='Learning rate', type=float, default=0.3)
parser.add_argument('-decay_rate', help='Learning rate decay', type=float, default=0.001)
parser.add_argument('-batchsize', help='Batchsize', type=int, default=100)
parser.add_argument('-modeldir', help='Folder for trained models', type=str, default=modeldir)
parser.add_argument('-cuda_id', help='Specify GPU', type=int, default=0)
parser.add_argument('-gene2id', help='Gene to ID mapping file', type=str, default=inputdir+"gene2ind.txt")
parser.add_argument('-drug2id', help='Drug to ID mapping file', type=str, default=inputdir+"drug2ind.txt")
parser.add_argument('-cell2id', help='Cell to ID mapping file', type=str, default=inputdir+"cell2ind.txt")

parser.add_argument('-number_neurons_per_GO', help='Mapping for the number of neurons in each term in genotype parts', type=int, default=6)
parser.add_argument('-number_neurons_per_final_GO', help='Mapping for the number of neurons in the root term', type=int, default=30)
parser.add_argument('-drug_neurons', help='Mapping for the number of neurons in each layer', type=str, default='200,100,15')
parser.add_argument('-final_neurons', help='The number of neurons in the top layer (before the output and after concatenating)', type=int, default=20)

parser.add_argument('-genotype', help='Mutation information for cell lines', type=str, default=inputdir+mutation)
parser.add_argument('-fingerprint', help='Morgan fingerprint representation for drugs', type=str, default=inputdir+"drug2fingerprint.txt")

parser.add_argument('-predict', help='Dataset to be predicted', type=str, default=inputdir+"drugcell_test.txt")
parser.add_argument('-result', help='Result file name', type=str, default=modeldir)

parser.add_argument('-project', help='Project name', type=str, default="prueba")
parser.add_argument('-gpu_name', help='GPU type', type=str, default="'RTX_A4000'")


# call functions
opt = parser.parse_args()
torch.set_printoptions(precision=5)

# Load ontology: create the graph of connected GO terms
since1 = time.time()
gene2id_mapping = load_mapping(opt.gene2id)
dG, terms_pairs, genes_terms_pairs = load_ontology(opt.onto, gene2id_mapping)
time_elapsed1 = time.time() - since1
print('Load ontology complete in {:.0f}m {:.0f}s'.format(
 time_elapsed1 // 60, time_elapsed1 % 60))
####

# Layer connections contains the pairs on each layer (including virtual nodes)
since2 = time.time()
sorted_pairs, level_list, level_number = sort_pairs(genes_terms_pairs, terms_pairs, dG, gene2id_mapping)
layer_connections = pairs_in_layers(sorted_pairs, level_list, level_number)
time_elapsed2 = time.time() - since2
print('\nLayer connections complete in {:.0f}m {:.0f}s'.format(
 time_elapsed2 // 60, time_elapsed2 % 60))
####

# load cell/drug features
cell_features = np.genfromtxt(opt.genotype, delimiter=',')
drug_features = np.genfromtxt(opt.fingerprint, delimiter=',')
drug_dim = len(drug_features[0,:])


since4 = time.time()
train_data = prepare_train_data(opt.train, opt.val, opt.cell2id, opt.drug2id)
time_elapsed4 = time.time() - since4
print('\nTrain data was ready in {:.0f}m {:.0f}s'.format(
 time_elapsed4 // 60, time_elapsed4 % 60))
####



# PREDICT/TEST DATA!
predict_data, cell2id_mapping, drug2id_mapping = prepare_predict_data(opt.predict, opt.cell2id, opt.drug2id)

num_cells = len(cell2id_mapping)
num_drugs = len(drug2id_mapping)
num_genes = len(gene2id_mapping)


# Configure the sweep – specify the parameters to search through, the search strategy, the optimization metric et all.
sweep_config = {
    'method': 'bayes', #grid, random

    'metric': {
      'name': 'correlation',
      'goal': 'maximize'
    },

    'parameters': {
        'epochs': {
            'value': opt.epoch
        },
        'batch_size': {
            #'values': [12000,13000,14000,15000]
            #'values': [15000,18000,21000,30000]
            'values': [15000] # 15000 ese el normal
        },
        'learning_rate': {
        # a flat distribution between 0 and 0.1
            #'distribution': 'uniform',
            #'min': 0.01,
            #'max': 0.1
            #'value': 0.1 # MUTACIONES
            #'value': 0.2 # EXPRESION
            'value': 0.1
            #'values': [0.001,0.01,0.02,0.1,0.2]
        },
        'optimizer': {
            #'values': ['adam', 'sgd']
            'value': 'sgd'
        },
        'num_neurons_per_GO': {
            #'values': [4,5,6,7]
            'value': 6
        },
        'num_neurons_per_final_GO': {
            # 'distribution': 'int_uniform',
            # 'min': 6,
            # 'max': 40
            'value': 30 # ESTE ES EL DE EXPRESION
            #'value': 6 # ESTE ES EL DE MUTACIONES
        },
        # 'num_neurons_final': {
        #     'distribution': 'int_uniform',
        #     'min': 12,
        #     'max': 50
        # },
        'num_neurons_drug_final': {
            # 'distribution': 'int_uniform',
            # 'min': 15,
            # 'max': 50
            'value': 50 # ESTE ES EL DE EXPRESION
            #'value': 6 # ESTE ES EL DE MUTACIONES
        },
        'decay_rate': {
            #'values': [0, 0.01,0.001]
            'value': 0.001
        },
        'criterion': {
            #'values': ['MSELoss', 'L1Loss']
            'value': 'MSELoss'
        },
        'gpu': {
            'value': opt.gpu_name
            #'value': 'RTX_A4000'
        },
        'drug_neurons': {
            'value': opt.drug_neurons
            #'value': 'RTX_A4000'
        },
        'momentum': {
            #'distribution': 'uniform',
            #'min': 0.9,
            #'max': 0.95
            'value':0.9
            #'values': [0.9,0.92,0.95]
        }

    }
}



# Initialize a new sweep
# Arguments:
#     – sweep_config: the sweep config dictionary defined above
#     – entity: Set the username for the sweep
#     – project: Set the project name for the sweep
sweep_id = wandb.sweep(sweep_config, entity="katynasada", project=opt.project)
def pipeline():
    # Initialize a new wandb run
    run = wandb.init(settings=wandb.Settings(start_method="thread"))

    # Config is a variable that holds and saves hyperparameters and inputs
    config = wandb.config

    num_neurons_per_GO = config.num_neurons_per_GO # neurons for each term
    num_neurons_per_final_GO = config.num_neurons_per_final_GO # neurons of final term (root term)
    num_neurons_drug = list(map(int, config.drug_neurons.split(','))) # neurons of drug layers
    num_neurons_drug[2] = config.num_neurons_drug_final

    num_neurons_final = round((num_neurons_drug[2]+num_neurons_per_final_GO)/2) # ESTE ES EL DE EXPRESION
    #num_neurons_final =12 # ESTE ES EL DE MUTACIONES

    ###

    ## Training
    since3 = time.time()
    #np.random.shuffle(layer_connections[0][:,1]) # shuffle genes
    model = sparseGO_nn(layer_connections,num_neurons_per_GO, num_neurons_per_final_GO, num_neurons_drug, num_neurons_final, drug_dim)
    time_elapsed3 = time.time() - since3
    print('\nModel created in {:.0f}m {:.0f}s'.format(
     time_elapsed3 // 60, time_elapsed3 % 60))
    ####

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if torch.cuda.device_count() > 1:
        print("Let's use", torch.cuda.device_count(), "GPUs!") # dim = 0 [30, xxx] -> [10, ...], [10, ...], [10, ...] on 3 GPUs
        model = nn.DataParallel(model)

    model.to(device)

    if config.optimizer=='sgd':
        momentum=config.momentum # momentum=0.9 SIEMPRE HABIA PUESTO 0.9
        print("Momentum: ", momentum)
        optimizer = torch.optim.SGD(model.parameters(), lr=config.learning_rate, momentum=momentum)
    elif config.optimizer=='adam':
        optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate, betas=(0.9, 0.99), eps=1e-05)

    if config.criterion=='MSELoss':
        criterion = nn.MSELoss()
        test_model = '/best_model_s.pt' # Test model is spearman
    elif config.criterion=='L1Loss':
        criterion = nn.L1Loss()
        test_model = '/best_model_p.pt' # Test model is pearson

    batch_size = config.batch_size
    decay_rate = config.decay_rate

    print("Decay rate: ", decay_rate)

    train_model(run,config, model, optimizer, criterion, train_data, cell_features, drug_features, batch_size, opt.modeldir, opt.cuda_id, config.epochs, decay_rate)

    since_test = time.time()
    predict("ModelSpearman",run, criterion, predict_data, num_genes, drug_dim, opt.modeldir + test_model, batch_size, opt.result, cell_features, drug_features, opt.cuda_id)
    predict("ModelLoss",run, criterion, predict_data, num_genes, drug_dim, opt.modeldir + '/best_model_l.pt', batch_size, opt.result, cell_features, drug_features, opt.cuda_id)

    time_elapsed_test = time.time() - since_test
    print('\nTest complete in {:.0f}m {:.0f}s'.format(
    time_elapsed_test // 60, time_elapsed_test % 60))

    time_elapsed0 = time.time() - since0
    print('\nTotal run time {:.0f}m {:.0f}s'.format(
        time_elapsed0 // 60, time_elapsed0 % 60))

    wandb.log({"time": '{:.0f}m {:.0f}s'.format(time_elapsed0 // 60, time_elapsed0 % 60)})

# RUUUUUUUN
wandb.agent(sweep_id, pipeline,count=1)

####
