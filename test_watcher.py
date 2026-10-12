import unittest
from watcher import VisibleText, normalize, classify

class Tests(unittest.TestCase):
    def test_waiting(self):
        self.assertEqual(classify(normalize('Votre plateforme dédiée aux vols spéciaux vers le Bénin sera bientôt disponible.')), 'waiting')
    def test_booking(self):
        self.assertEqual(classify(normalize('RÉSERVEZ VOTRE VOL')), 'booking_possible')
    def test_unknown(self):
        self.assertEqual(classify(normalize('Nouvelle page avec informations')), 'changed_unknown')
    def test_hidden_script(self):
        p = VisibleText(); p.feed('<script>Réservez votre vol</script><main>Bientôt disponible</main>')
        self.assertEqual(normalize(' '.join(p.parts)), 'bientôt disponible')

if __name__ == '__main__': unittest.main()
