#include "Collector.h"  //copied from selfplay
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

// to write file
#include <stdio.h>

#include "Collector.h"  //copy end
#include "Config.h"
#include "TimeControl.h"
#include "Tree.h"
#include "Util.h"

// TCP socket network:
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <cstdio>
#include <cstring>

// capnp
#include <capnp/message.h>
#include <capnp/serialize.h>
#include "CapnpGame.capnp.h"

#include <ctime>

#include <boost/lexical_cast.hpp>
#include <boost/uuid/uuid.hpp>
#include <boost/uuid/uuid_generators.hpp>
#include <boost/uuid/uuid_io.hpp>

Collector::Collector(const char* server, int port) {
    // initialize all the variables
    _server = server;
    _port = port;

    // initialize capnp message if data should be collected
    std::vector<std::array<double, BOARDSIZE * BOARDSIZE + 1>> _probabilities;
    std::vector<NetworkFeatures::Planes> _states;
    std::vector<double> _winrates;
}

void Collector::collectMove(const Tree& tree) {
    static const size_t num_fields = BOARDSIZE * BOARDSIZE + 1;

    std::array<double, num_fields> node_probs;
    std::int16_t pos, row, col, board_size;     // where in the array to write the probability

    for (size_t i = 0; i < num_fields; ++i) {   // initialize probabilities with zero
        node_probs[i] = 0.f;
    }

    // iterate over all node. only legal moves do exist.
    for (auto node : tree.rootNode()->children().get()) {
        // is vertex a pass
        if (node->parentMove().GetRaw() == Vertex::Pass().GetRaw()) {
            pos = BOARDSIZE * BOARDSIZE;
        } else {
            row = node->parentMove().GetRow();
            col = node->parentMove().GetColumn();
            board_size = BOARDSIZE;

            pos = row * board_size + col;
        }

        // write the statistics to the array
        node_probs[pos] = node->statistics().num_evaluations.load();
    }

    // normalize visits by dividing by the sum of all
    // get the sum
    int a, sum = 0;
    for (a = 0; a < num_fields; a++) {
        sum += node_probs[a];
    }

    // now divide
    for (a = 0; a < num_fields; a++) {
        node_probs[a] = node_probs[a] / sum;
    }

    // probabilities
    _probabilities.push_back(node_probs);

    // states from the board
    _states.push_back(tree.rootBoard().getFeatures().getPlanes());

    _winrates.push_back(tree.rootNode()->winrate(tree.rootBoard().ActPlayer()));

}

void Collector::collectWinner(const Player& winner) { _winner = winner; }

int Collector::connectToServer(const char* server, int port) {

    int sock = 0;
    struct sockaddr_in serv_addr;
    if ((sock = socket(AF_INET, SOCK_STREAM, 0)) < 0) {
        printf("\n Socket creation error \n");
        return -1;
    }

    memset(&serv_addr, '0', sizeof(serv_addr));

    serv_addr.sin_family = AF_INET;
    serv_addr.sin_port = htons(port);

    // Convert IPv4 and IPv6 addresses from text to binary form
    if (inet_pton(AF_INET, server, &serv_addr.sin_addr) <= 0) {
        printf("\nInvalid address/ Address not supported \n");
        return -1;
    }

    if (connect(sock, (struct sockaddr*)&serv_addr, sizeof(serv_addr)) < 0) {
        printf("\nConnection Failed \n");
        return -1;
    }

    int flag = 1;
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, (char *) &flag, sizeof(int));

    return sock;
}

void Collector::sendData(const Tree& tree, bool noResignMode) {

    // get the Socket fd for server at port
    int sockfd;
    sockfd = connectToServer(this->_server, this->_port);

    // serialize collected info into capnp Message
    static const size_t num_fields = BOARDSIZE * BOARDSIZE + 1;
    std::uint16_t a, b, row, col, f;
    std::uint16_t num_moves = _states.size();
    std::uint16_t num_features = NetworkFeatures::NUM_FEATURES;
    std::uint16_t num_flattened_features = board_size * board_size * num_features;

    ::capnp::MallocMessageBuilder message;
    Game::Builder game = message.initRoot<Game>();
    ::capnp::List<StateProb>::Builder stateprobs = game.initStateprobs(num_moves);

    // fill the StateProb structs with the information from the vectors
    for (a = 0; a < num_moves; a++) {
        StateProb::Builder stateprob = stateprobs[a];
        stateprob.setIdx(a);
        stateprob.setWinrate(_winrates[a]);

        // fill probs
        ::capnp::List<float>::Builder probs = stateprob.initProbs(num_fields);
        for (b = 0; b < num_fields; b++) {
            probs.set(b, _probabilities[a][b]);
        }

        // fill states
        ::capnp::List<float>::Builder cstates = stateprob.initState(num_flattened_features);
        for (f = 0; f < num_features; f++) {
            for (row = 0; row < board_size; row++) {
                for (col = 0; col < board_size; col++) {
                    const size_t ind = f * board_size * board_size + row * board_size + col;
                    cstates.set(ind, _states[a][f][row][col]);
                }
            }
        }
    }

    // now fill the game
    boost::uuids::uuid id = boost::uuids::random_generator()();
    game.setId(boost::lexical_cast<std::string>(id));

    std::time_t time = std::time(nullptr);
    game.setTimestamp(time);

    game.setBoardsize(board_size);

    auto netID = tree.configuration().networkPath.filename().string();
    game.setNetwork1(netID);
    game.setNetwork2(netID);

    game.setNoresignmode(noResignMode);

    assert(_winner.IsValid());
    int result = _winner.ToScore();  // ToScore (Black()) == 1, ToScore (White()) == -1
    int binarized_result = (result + 1) / 2;
    bool res = binarized_result;
    game.setResult(res);

    // for testing purpose write capnp files to disk, not to network
/*
    FILE* capnp_file;
    std::string path = "/home/franziska/";
    std::string filename = boost::lexical_cast<std::string>(id);
    std::cout << filename << std::endl;
    capnp_file = fopen((path + filename).c_str(), "w");
    if (capnp_file != NULL) {
        writeMessageToFd(fileno(capnp_file), message);
        fclose(capnp_file);
    }
*/



    std::cout << sizeof(message) << std::endl;
    // if valid file descriptor exists
    if(sockfd != -1){
        writeMessageToFd(sockfd, message);
    }

    //close socket
     close(sockfd);
}
