(defvar arr 3000)      
(defvar count 0)       
(defvar current-num 0) 
(defvar running 1)     
(defvar swapped 1)
(defvar n 0)
(defvar ch 0)
(defvar digit 0)

(def-interrupt
  (setq ch (in-char))
  (if (= ch 10)
      (progn
        (if (> current-num 0)
            (progn
              (setq count (+ count 1))
              (write-mem (+ arr count) current-num))
            (setq current-num current-num))
        (write-mem arr count)
        (setq running 0))
      (if (= ch 32)
          (progn
            (if (> current-num 0)
                (progn
                  (setq count (+ count 1))
                  (write-mem (+ arr count) current-num)
                  (setq current-num 0))
                (setq current-num 0)))
          (progn
            (setq digit (- ch 48))
            (setq current-num (+ (* current-num 10) digit))))))

(loop i 0 5000
  (if (= running 0)
      (setq i 5000)))

(setq n (read-mem arr))

(if (> n 1)
    (loop iter 0 (- n 1)
      (if (= swapped 0)
          (setq iter (- n 1))
          (progn
            (setq swapped 0)
            (loop j 0 (- (- n iter) 2)
              (progn
                (defvar v1 (read-mem (+ arr (+ j 1))))
                (defvar v2 (read-mem (+ arr (+ j 2))))
                (if (> v1 v2)
                    (progn
                      (write-mem (+ arr (+ j 1)) v2)
                      (write-mem (+ arr (+ j 2)) v1)
                      (setq swapped 1))
                    (setq swapped swapped))))))))

(if (= n 0)
    (print-pstr "empty")
    (loop k 1 n
      (progn
        (print (read-mem (+ arr k)))
        (print-char 32))))