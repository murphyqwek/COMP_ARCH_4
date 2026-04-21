(defvar A-Low 4000000000)
(defvar A-High 1)
(defvar B-Low 1000000000)
(defvar B-High 2)

(defvar Res-Low (+ A-Low B-Low))
(defvar Res-High (adc A-High B-High))

(print Res-High)
(print-pstr " ")
(print Res-Low)