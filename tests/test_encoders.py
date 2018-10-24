import pytest

import remoulade


@pytest.fixture
def pickle_encoder():
    old_encoder = remoulade.get_encoder()
    new_encoder = remoulade.PickleEncoder()
    remoulade.set_encoder(new_encoder)
    yield new_encoder
    remoulade.set_encoder(old_encoder)


def test_set_encoder_sets_the_global_encoder(pickle_encoder):
    # Given that I've set a Pickle encoder as the global encoder
    # When I get the global encoder
    encoder = remoulade.get_encoder()

    # Then it should be the same as the encoder that was set
    assert encoder == pickle_encoder


def test_pickle_encoder(pickle_encoder, stub_broker, stub_worker):
    # Given that I've set a Pickle encoder as the global encoder
    # And I have an actor that adds a value to a db
    db = []

    @remoulade.actor
    def add_value(x):
        db.append(x)

    # When I send that actor a message
    add_value.send(1)

    # And wait on the broker and worker
    stub_broker.join(add_value.queue_name)
    stub_worker.join()

    # Then I expect the message to have been processed
    assert db == [1]
