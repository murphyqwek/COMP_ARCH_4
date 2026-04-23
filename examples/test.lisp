(defvar x 1)
(defvar a 5)

(print (setq x 13))
(print-pstr " ")

(setq x (if (= x 13) 100 200))
(print x)
(print-pstr " ")

(setq x
    (loop i 1 5
        (+ i 10)))
        
(print x)
(print-pstr " ")

(print (+ (if (= a 5) 10 0) (setq a 5)))
(print-pstr " ")