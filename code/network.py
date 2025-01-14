import sys
import os
import numpy as np
import torch
import torch.utils.data as du
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
from scipy import sparse # for genes_layer
import torch_sparse
import util
from util import *
from sparseLinearNew import SparseLinearNew

class sparseGO_nn(nn.Module):

    def __init__(self,layer_connections,num_neurons_per_GO, num_neurons_per_final_GO, num_neurons_drug, num_neurons_final, drug_dim,gene2id_mapping):

        super(sparseGO_nn, self).__init__()

        self.num_neurons_per_GO = num_neurons_per_GO
        self.num_neurons_per_final_GO = num_neurons_per_final_GO
        self.num_neurons_drug = num_neurons_drug
        self.drug_dim = drug_dim
        self.layer_connections=layer_connections
        print("\nNumber of neurons per GO term: ", num_neurons_per_GO)
        print("Number of neurons of final GO term: ", num_neurons_per_final_GO)
        print("Number of drug neurons: ", num_neurons_drug)
        print("Number of final neurons: ", num_neurons_final)

        # (1) Layer of genes with terms
        input_id = self.genes_layer(layer_connections[0],gene2id_mapping)

        print("Number of term-term hierarchy levels:", len(layer_connections))
        # (2...) Layers of terms with terms
        for i in range(1,len(layer_connections)):
            if i == len(layer_connections)-1:
                input_id = self.terms_layer(input_id, layer_connections[i], str(i),num_neurons_per_final_GO)
            else:
                input_id = self.terms_layer(input_id, layer_connections[i], str(i),num_neurons_per_GO)

        # Add modules for neural networks to process drugs
        self.construct_NN_drug()

        # Add modules for final layer
        final_input_size = num_neurons_per_final_GO + num_neurons_drug[-1]
        self.add_module('final_linear_layer', nn.Linear(final_input_size, num_neurons_final))
        self.add_module('final_tanh', nn.Tanh())
        self.add_module('final_batchnorm_layer', nn.BatchNorm1d(num_neurons_final))
        self.add_module('final_aux_linear_layer', nn.Linear(num_neurons_final,1))
        self.add_module('final_aux_tanh', nn.Tanh())
        self.add_module('final_linear_layer_output', nn.Linear(1, 1))

    def genes_layer(self, genes_terms_pairs, gene2id):
        # Define the layer of terms with genes, each pair is repeated 6 times (for the 6 neurons)

        term2id = create_index(genes_terms_pairs[:,0])
        
        self.gene_dim = len(gene2id)
        self.term_dim = len(term2id)

        # change term and genes to its indexes
        rows = [term2id[term] for term in genes_terms_pairs[:,0]]
        columns = [gene2id[gene] for gene in genes_terms_pairs[:,1]]

        data = np.ones(len(rows))

        # Create sparse matrix of terms connected to genes (2068 x 3008)
        genes_terms = sparse.coo_matrix((data, (rows, columns)), shape=(self.term_dim, self.gene_dim))

        # Add 6 neurons to each term ((2068x6) x 3008)
        genes_terms_more_neurons = sparse.lil_matrix((self.term_dim*self.num_neurons_per_GO, self.gene_dim))
        genes_terms = genes_terms.tolil()
        # Repeat the rows of the sparse matrix to match the 6 neurons
        row=0
        for i in range(genes_terms_more_neurons.shape[0]):
            if (i != 0) and (i%self.num_neurons_per_GO) == 0 :
                row=row+1
            genes_terms_more_neurons[i,:]=genes_terms[row,:]

        # get the indexes of the matrix to define the connections of the sparse layer
        rows_more_neurons = torch.from_numpy(sparse.find(genes_terms_more_neurons)[0]).view(1,-1).long()
        columns_more_neurons = torch.from_numpy(sparse.find(genes_terms_more_neurons)[1]).view(1,-1).long()
        connections_layer1 = torch.cat((rows_more_neurons, columns_more_neurons), dim=0) # connections of the first layer each gene-term pair is repeated 6 times

        input_terms = len(gene2id)
        output_terms = self.num_neurons_per_GO*len(term2id) # 6 * GOterms
        self.add_module('genes_terms_sparse_linear_1', SparseLinearNew(input_terms, output_terms, connectivity=connections_layer1))
        self.add_module('genes_terms_tanh', nn.Tanh())
        self.add_module('genes_terms_batchnorm', nn.BatchNorm1d(output_terms))

        return term2id

    def terms_layer(self, input_id, layer_pairs, number,neurons_per_GO):

        output_id = create_index(layer_pairs[:,0])

        # change term and genes to its indexes
        rows = [output_id[term] for term in layer_pairs[:,0]]
        columns = [input_id[term] for term in layer_pairs[:,1]]

        data = np.ones(len(rows))

        # Create sparse matrix of terms connected to terms
        connections_matrix = sparse.coo_matrix((data, (rows, columns)), shape=(len(output_id), len(input_id)))

        # Add the 6 (or n) neurons with kronecker
        ones = sparse.csr_matrix(np.ones([neurons_per_GO, self.num_neurons_per_GO], dtype = int))
        connections_matrix_more_neurons = sparse.csr_matrix(sparse.kron(connections_matrix, ones))

        # Find the rows and columns of the connections
        rows_more_neurons = torch.from_numpy(sparse.find(connections_matrix_more_neurons)[0]).view(1,-1).long()
        columns_more_neurons = torch.from_numpy(sparse.find(connections_matrix_more_neurons)[1]).view(1,-1).long()
        connections = torch.cat((rows_more_neurons, columns_more_neurons), dim=0)

        input_terms = self.num_neurons_per_GO*len(input_id)
        output_terms = neurons_per_GO*len(output_id)
        self.add_module('GO_terms_sparse_linear_'+number, SparseLinearNew(input_terms, output_terms, connectivity=connections))
        self.add_module('GO_terms_tanh_'+number, nn.Tanh())
        self.add_module('GO_terms_batchnorm_'+number, nn.BatchNorm1d(output_terms))


        return output_id

    # add modules for fully connected neural networks for drug processing
    def construct_NN_drug(self):
        input_size = self.drug_dim

        for i in range(len(self.num_neurons_drug)):
            self.add_module('drug_linear_layer_' + str(i+1), nn.Linear(input_size, self.num_neurons_drug[i]))
            self.add_module('drug_tanh_' + str(i+1), nn.Tanh())
            self.add_module('drug_batchnorm_layer_' + str(i+1), nn.BatchNorm1d(self.num_neurons_drug[i]))
            input_size = self.num_neurons_drug[i]

    # definition of forward function
    def forward(self, x):
        gene_input = x.narrow(1, 0, self.gene_dim) # genes features (Returns a new tensor that is a narrowed version)
        drug_input = x.narrow(1, self.gene_dim, self.drug_dim) # drugs features

        # define forward function for GO terms and genes #############################################

        # (1) Layer 1 + tanh
        gene_output = self._modules['genes_terms_tanh'](self._modules['genes_terms_sparse_linear_1'](gene_input))
        terms_output  = (self._modules['genes_terms_batchnorm'](gene_output))

        # (2...) Layer 2,3,4... + tanh
        for i in range(1,len(self.layer_connections)):
            terms_output = self._modules['GO_terms_tanh_'+str(i)](self._modules['GO_terms_sparse_linear_'+str(i)](terms_output))
            terms_output = (self._modules['GO_terms_batchnorm_'+str(i)](terms_output))

        # define forward function for drugs #################################################
        drug_out = drug_input

        for i in range(1, len(self.num_neurons_drug)+1, 1):
            drug_out = self._modules['drug_batchnorm_layer_'+str(i)](self._modules['drug_tanh_'+str(i)](self._modules['drug_linear_layer_' + str(i)](drug_out)))

        # connect two neural networks #################################################
        final_input = torch.cat((terms_output, drug_out), 1)

        output = self._modules['final_batchnorm_layer'](self._modules['final_tanh'](self._modules['final_linear_layer'](final_input)))
        output = self._modules['final_aux_tanh'](self._modules['final_aux_linear_layer'](output))
        final_output = self._modules['final_linear_layer_output'](output)

        return final_output
